package com.touguang.registry;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;
import org.springframework.http.HttpStatus;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@RestController
@RequestMapping("/api/v1/registry")
public class RegistryController {
    private final JdbcTemplate jdbc;
    private final TransactionTemplate transactions;

    public RegistryController(JdbcTemplate jdbc, TransactionTemplate transactions) {
        this.jdbc = jdbc;
        this.transactions = transactions;
    }

    public record Candidate(
            @NotBlank @Size(max = 96) String productId,
            @NotBlank @Size(max = 96) String leafCategoryId,
            @Size(max = 512) String title) {}
    public record ReserveRequest(
            @NotBlank String requestId,
            @NotBlank @Size(max = 128) String accountKey,
            @NotEmpty @Size(max = 50) List<@Valid Candidate> items) {}
    public record FinishRequest(@NotBlank String outcome) {}
    public record ResolveRequest(@NotBlank String outcome, boolean manuallyConfirmed) {}
    public record ImportItem(
            @NotBlank @Size(max = 96) String productId,
            @Size(max = 128) String accountKey,
            @Size(max = 96) String leafCategoryId,
            @Size(max = 512) String title) {}
    public record ImportRequest(@NotEmpty @Size(max = 5000) List<@Valid ImportItem> items) {}

    @GetMapping("/health")
    public Map<String, Object> health() {
        jdbc.queryForObject("SELECT 1", Integer.class);
        return Map.of("ok", true, "service", "selection-registry");
    }

    @GetMapping("/status")
    public Map<String, Object> status() {
        jdbc.queryForObject("SELECT 1", Integer.class);
        return Map.of("ok", true, "service", "selection-registry");
    }

    @PostMapping("/snapshot")
    public Map<String, Object> snapshot(@Valid @RequestBody SnapshotRequest request) {
        List<String> requestedProducts = normalizedIds(request.productIds());
        List<String> products = requestedProducts.isEmpty() ? List.of() : jdbc.query(
                "SELECT product_id FROM selection_reservation WHERE state <> 'RELEASED' AND product_id IN (" + placeholders(requestedProducts.size()) + ")",
                (rs, row) -> rs.getString(1), requestedProducts.toArray());
        List<String> categories = jdbc.query(
                "SELECT leaf_category_id FROM account_category_history WHERE account_key = ? AND state <> 'RELEASED'",
                (rs, row) -> rs.getString(1), request.accountKey());
        Map<String, Long> usage = new LinkedHashMap<>();
        List<String> requestedCategories = normalizedIds(request.leafCategoryIds());
        if (!requestedCategories.isEmpty()) {
            String sql = "SELECT leaf_category_id, COUNT(*) AS n FROM account_category_history " +
                    "WHERE state IN ('ATTEMPTING','COMMITTED','UNCERTAIN') AND leaf_category_id IN (" +
                    placeholders(requestedCategories.size()) + ") GROUP BY leaf_category_id";
            jdbc.query(sql, (RowCallbackHandler) rs -> usage.put(rs.getString("leaf_category_id"), rs.getLong("n")),
                    requestedCategories.toArray());
        }
        return Map.of("usedProductIds", products, "accountUsedCategoryIds", categories, "categoryUsage", usage);
    }

    private List<String> normalizedIds(List<String> values) {
        if (values == null || values.isEmpty()) return List.of();
        return values.stream().map(String::trim).filter(value -> !value.isEmpty()).distinct().toList();
    }

    private String placeholders(int count) {
        return String.join(",", java.util.Collections.nCopies(count, "?"));
    }

    public record SnapshotRequest(
            @NotBlank @Size(max = 128) String accountKey,
            @Size(max = 500) List<@NotBlank @Size(max = 96) String> productIds,
            @Size(max = 500) List<@NotBlank @Size(max = 96) String> leafCategoryIds) {}

    @PostMapping("/reservations")
    public Map<String, Object> reserve(@Valid @RequestBody ReserveRequest request) {
        UUID requestId = parseUuid(request.requestId(), "requestId");
        List<Map<String, Object>> reserved = new ArrayList<>();
        List<Map<String, Object>> rejected = new ArrayList<>();
        for (Candidate item : request.items()) {
            Map<String, Object> outcome = transactions.execute(status -> reserveOne(requestId, request.accountKey(), item));
            if (Boolean.TRUE.equals(outcome.get("reserved"))) reserved.add(outcome);
            else rejected.add(outcome);
        }
        return Map.of("reserved", reserved, "rejected", rejected);
    }

    private Map<String, Object> reserveOne(UUID requestId, String account, Candidate item) {
        String productId = item.productId().trim();
        String categoryId = item.leafCategoryId().trim();
        List<Map<String, Object>> retry = jdbc.query(
                "SELECT reservation_token,state,account_key,leaf_category_id FROM selection_reservation WHERE request_id=? AND product_id=?",
                (rs, row) -> Map.of("token", rs.getString(1), "state", rs.getString(2), "account", rs.getString(3), "category", rs.getString(4)),
                requestId.toString(), productId);
        if (!retry.isEmpty()) {
            Map<String, Object> previous = retry.get(0);
            if (account.equals(previous.get("account")) && categoryId.equals(previous.get("category")) && !"RELEASED".equals(previous.get("state")))
                return Map.of("reserved", true, "productId", productId, "token", previous.get("token"), "state", previous.get("state"));
            return rejected(productId, "REQUEST_CONFLICT");
        }
        List<String> productStates = jdbc.query("SELECT state FROM selection_reservation WHERE product_id=?", (rs, row) -> rs.getString(1), productId);
        if (!productStates.isEmpty() && !"RELEASED".equals(productStates.get(0))) return rejected(productId, "PRODUCT_ALREADY_REGISTERED");
        List<String> categoryStates = jdbc.query(
                "SELECT state FROM account_category_history WHERE account_key=? AND leaf_category_id=?",
                (rs, row) -> rs.getString(1), account, categoryId);
        if (!categoryStates.isEmpty() && !"RELEASED".equals(categoryStates.get(0))) return rejected(productId, "ACCOUNT_CATEGORY_ALREADY_USED");

        String token = UUID.randomUUID().toString();
        try {
            jdbc.update("INSERT INTO account_category_history(account_key,leaf_category_id,product_id,state) VALUES(?,?,?,'RESERVED')",
                    account, categoryId, productId);
        } catch (DuplicateKeyException conflict) {
            return rejected(productId, "ACCOUNT_CATEGORY_ALREADY_USED");
        }
        if (productStates.isEmpty()) {
            try {
                jdbc.update("INSERT INTO selection_reservation(product_id,reservation_token,request_id,account_key,leaf_category_id,title,state) VALUES(?,?,?,?,?,?, 'RESERVED')",
                        productId, token, requestId.toString(), account, categoryId, item.title());
            } catch (DuplicateKeyException conflict) {
                jdbc.update("DELETE FROM account_category_history WHERE account_key=? AND leaf_category_id=? AND product_id=? AND state='RESERVED'",
                        account, categoryId, productId);
                return rejected(productId, "PRODUCT_ALREADY_REGISTERED");
            }
        } else {
            int updated = jdbc.update("UPDATE selection_reservation SET reservation_token=?,request_id=?,account_key=?,leaf_category_id=?,title=?,state='RESERVED' WHERE product_id=? AND state='RELEASED'",
                    token, requestId.toString(), account, categoryId, item.title(), productId);
            if (updated != 1) {
                jdbc.update("DELETE FROM account_category_history WHERE account_key=? AND leaf_category_id=? AND product_id=? AND state='RESERVED'",
                        account, categoryId, productId);
                return rejected(productId, "PRODUCT_ALREADY_REGISTERED");
            }
        }
        return Map.of("reserved", true, "productId", productId, "token", token, "state", "RESERVED");
    }

    private Map<String, Object> rejected(String productId, String reason) {
        return Map.of("reserved", false, "productId", productId, "reason", reason);
    }

    @PostMapping("/reservations/{token}/begin")
    public Map<String, Object> begin(@PathVariable String token) {
        return transition(token, "RESERVED", "ATTEMPTING", "ATTEMPTING");
    }

    @PostMapping("/reservations/{token}/finish")
    public Map<String, Object> finish(@PathVariable String token, @Valid @RequestBody FinishRequest request) {
        String outcome = request.outcome().trim().toUpperCase();
        return switch (outcome) {
            case "ADDED" -> transition(token, "ATTEMPTING", "COMMITTED", "COMMITTED");
            case "NOT_ADDED" -> release(token);
            case "UNKNOWN" -> transition(token, "ATTEMPTING", "UNCERTAIN", "UNCERTAIN");
            default -> throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "outcome must be ADDED, NOT_ADDED or UNKNOWN");
        };
    }

    @GetMapping("/pending")
    public List<Map<String, Object>> pending() {
        return jdbc.query("SELECT product_id,reservation_token,account_key,leaf_category_id,title,state,updated_at FROM selection_reservation WHERE state IN ('RESERVED','ATTEMPTING','UNCERTAIN') ORDER BY updated_at",
                (rs, row) -> {
                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("productId", rs.getString("product_id"));
                    item.put("token", rs.getString("reservation_token"));
                    item.put("accountKey", rs.getString("account_key"));
                    item.put("leafCategoryId", rs.getString("leaf_category_id"));
                    item.put("title", rs.getString("title"));
                    item.put("state", rs.getString("state"));
                    item.put("updatedAt", rs.getTimestamp("updated_at").toInstant().toString());
                    return item;
                });
    }

    @PostMapping("/reservations/{token}/resolve")
    public Map<String, Object> resolve(@PathVariable String token, @Valid @RequestBody ResolveRequest request) {
        if (!request.manuallyConfirmed()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "manual confirmation is required");
        String outcome = request.outcome().trim().toUpperCase();
        UUID uuid = parseUuid(token, "token");
        return transactions.execute(status -> {
            List<Map<String, Object>> rows = jdbc.query("SELECT product_id,state FROM selection_reservation WHERE reservation_token=? FOR UPDATE",
                    (rs, row) -> Map.of("productId", rs.getString(1), "state", rs.getString(2)), uuid.toString());
            if (rows.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND, "reservation not found");
            Map<String, Object> row = rows.get(0);
            if (!List.of("RESERVED", "ATTEMPTING", "UNCERTAIN").contains(row.get("state")))
                throw new ResponseStatusException(HttpStatus.CONFLICT, "reservation is already final");
            if (outcome.equals("ADDED")) {
                jdbc.update("UPDATE selection_reservation SET state='COMMITTED' WHERE reservation_token=?", uuid.toString());
                jdbc.update("UPDATE account_category_history SET state='COMMITTED' WHERE product_id=?", row.get("productId"));
                return Map.of("ok", true, "state", "COMMITTED", "idempotent", false);
            }
            if (outcome.equals("NOT_ADDED")) {
                jdbc.update("UPDATE selection_reservation SET state='RELEASED' WHERE reservation_token=?", uuid.toString());
                jdbc.update("DELETE FROM account_category_history WHERE product_id=?", row.get("productId"));
                return Map.of("ok", true, "state", "RELEASED", "idempotent", false);
            }
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "outcome must be ADDED or NOT_ADDED");
        });
    }

    private Map<String, Object> transition(String token, String expected, String next, String categoryState) {
        UUID uuid = parseUuid(token, "token");
        return transactions.execute(status -> {
            List<Map<String, Object>> rows = jdbc.query("SELECT product_id,state FROM selection_reservation WHERE reservation_token=? FOR UPDATE",
                    (rs, row) -> Map.of("productId", rs.getString(1), "state", rs.getString(2)), uuid.toString());
            if (rows.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND, "reservation not found");
            Map<String, Object> row = rows.get(0);
            if (next.equals(row.get("state"))) return Map.of("ok", true, "state", next, "idempotent", true);
            if (!expected.equals(row.get("state"))) throw new ResponseStatusException(HttpStatus.CONFLICT, "reservation state conflict: " + row.get("state"));
            jdbc.update("UPDATE selection_reservation SET state=? WHERE reservation_token=?", next, uuid.toString());
            jdbc.update("UPDATE account_category_history SET state=? WHERE product_id=?", categoryState, row.get("productId"));
            return Map.of("ok", true, "state", next, "idempotent", false);
        });
    }

    private Map<String, Object> release(String token) {
        UUID uuid = parseUuid(token, "token");
        return transactions.execute(status -> {
            List<Map<String, Object>> rows = jdbc.query("SELECT product_id,state FROM selection_reservation WHERE reservation_token=? FOR UPDATE",
                    (rs, row) -> Map.of("productId", rs.getString(1), "state", rs.getString(2)), uuid.toString());
            if (rows.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND, "reservation not found");
            Map<String, Object> row = rows.get(0);
            String state = (String) row.get("state");
            if ("RELEASED".equals(state)) return Map.of("ok", true, "state", "RELEASED", "idempotent", true);
            if (!"ATTEMPTING".equals(state) && !"RESERVED".equals(state)) throw new ResponseStatusException(HttpStatus.CONFLICT, "cannot release reservation in state " + state);
            jdbc.update("UPDATE selection_reservation SET state='RELEASED' WHERE reservation_token=?", uuid.toString());
            jdbc.update("DELETE FROM account_category_history WHERE product_id=? AND state IN ('RESERVED','ATTEMPTING')", row.get("productId"));
            return Map.of("ok", true, "state", "RELEASED", "idempotent", false);
        });
    }

    @PostMapping("/import")
    public Map<String, Object> importHistory(@Valid @RequestBody ImportRequest request) {
        int imported = 0;
        for (ImportItem item : request.items()) {
            transactions.executeWithoutResult(status -> {
                int count = jdbc.update("INSERT IGNORE INTO selection_reservation(product_id,reservation_token,request_id,account_key,leaf_category_id,title,state) VALUES(?,?,?,?,?,?, 'COMMITTED')",
                        item.productId().trim(), UUID.randomUUID().toString(), UUID.randomUUID().toString(),
                        item.accountKey() == null || item.accountKey().isBlank() ? "legacy-unmapped" : item.accountKey().trim(),
                        item.leafCategoryId() == null || item.leafCategoryId().isBlank() ? "legacy-unmapped-" + item.productId().trim() : item.leafCategoryId().trim(), item.title());
                if (count == 1 && item.accountKey() != null && !item.accountKey().isBlank() && item.leafCategoryId() != null && !item.leafCategoryId().isBlank()) {
                    jdbc.update("INSERT IGNORE INTO account_category_history(account_key,leaf_category_id,product_id,state) VALUES(?,?,?,'COMMITTED')",
                            item.accountKey().trim(), item.leafCategoryId().trim(), item.productId().trim());
                }
            });
            imported++;
        }
        return Map.of("received", request.items().size(), "processed", imported);
    }

    private UUID parseUuid(String value, String field) {
        try { return UUID.fromString(value); }
        catch (RuntimeException error) { throw new ResponseStatusException(HttpStatus.BAD_REQUEST, field + " must be a UUID"); }
    }

    @RestControllerAdvice
    static class Errors {
        @ExceptionHandler(org.springframework.dao.DuplicateKeyException.class)
        @ResponseStatus(HttpStatus.CONFLICT)
        Map<String, String> duplicate() { return Map.of("error", "product or account category already reserved"); }
    }
}
