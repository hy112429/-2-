package com.touguang.center.service;

import com.touguang.center.common.BusinessException;
import com.touguang.center.service.AuthService.ClientBindingException;
import com.touguang.center.dto.LicenseDtos.HeartbeatRequest;
import com.touguang.center.dto.LicenseDtos.LicenseView;
import com.touguang.center.entity.ClientEntity;
import com.touguang.center.entity.LicenseEntity;
import com.touguang.center.entity.LicenseOperationEntity;
import com.touguang.center.repository.mapper.ClientMapper;
import com.touguang.center.repository.mapper.HeartbeatMapper;
import com.touguang.center.repository.mapper.LicenseMapper;
import com.touguang.center.repository.mapper.LicenseOperationMapper;
import java.time.Clock;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class LicenseService {
    public static final long HEARTBEAT_INTERVAL_SECONDS = 60;
    public static final long OFFLINE_GRACE_SECONDS = 600;
    private static final long EXPIRING_SECONDS = 3 * 24 * 60 * 60;

    private final ClientMapper clients;
    private final LicenseMapper licenses;
    private final LicenseOperationMapper operations;
    private final HeartbeatMapper heartbeats;
    private final Clock clock;

    public LicenseService(ClientMapper clients, LicenseMapper licenses, LicenseOperationMapper operations,
                          HeartbeatMapper heartbeats, Clock clock) {
        this.clients = clients;
        this.licenses = licenses;
        this.operations = operations;
        this.heartbeats = heartbeats;
        this.clock = clock;
    }

    @Transactional(readOnly = true)
    public LicenseView get(long clientId) {
        if (clients.selectById(clientId) == null) throw new BusinessException(40003, "客户端不存在");
        return view(clientId, licenses.findByClientId(clientId), now());
    }

    @Transactional
    public LicenseView grant(long clientId, int days, UUID requestId, long actorUserId) {
        ClientEntity client = requireBoundClient(clientId);
        LicenseEntity license = licenses.findByClientId(clientId);
        if (alreadyApplied(requestId, clientId, "GRANT")) return view(clientId, license, now());
        if (license != null && !"DISABLED".equals(license.getStatus())) {
            throw new BusinessException(40020, "该机器已有授权，请使用续期接口");
        }
        LocalDateTime now = now();
        LocalDateTime expiresAt = now.plusDays(days);
        LocalDateTime previous = license == null ? null : license.getExpiresAt();
        if (license == null) {
            license = new LicenseEntity();
            license.setClientId(client.getId());
            license.setGrantedDaysTotal((long) days);
            license.setCreatedAt(now);
        } else {
            license.setGrantedDaysTotal(license.getGrantedDaysTotal() + days);
        }
        license.setExpiresAt(expiresAt);
        license.setStatus("ACTIVE");
        license.setDisabledAt(null);
        license.setUpdatedAt(now);
        if (license.getId() == null) licenses.insert(license);
        else licenses.updateById(license);
        recordOperation(requestId, clientId, actorUserId, "GRANT", days, previous, expiresAt, now);
        return view(clientId, license, now);
    }

    @Transactional
    public LicenseView renew(long clientId, int days, UUID requestId, long actorUserId) {
        requireBoundClient(clientId);
        LicenseEntity license = licenses.findByClientId(clientId);
        if (alreadyApplied(requestId, clientId, "RENEW")) return view(clientId, license, now());
        if (license == null) throw new BusinessException(40021, "该机器尚未授予授权");
        if ("DISABLED".equals(license.getStatus())) {
            throw new BusinessException(40022, "该授权已停用，请使用授予接口重新启用");
        }
        if (!"ACTIVE".equals(license.getStatus())) throw new BusinessException(40025, "授权状态异常，请联系管理员");
        LocalDateTime now = now();
        LocalDateTime previous = license.getExpiresAt();
        LocalDateTime base = previous.isAfter(now) ? previous : now;
        license.setExpiresAt(base.plusDays(days));
        license.setGrantedDaysTotal(license.getGrantedDaysTotal() + days);
        license.setUpdatedAt(now);
        licenses.updateById(license);
        recordOperation(requestId, clientId, actorUserId, "RENEW", days, previous, license.getExpiresAt(), now);
        return view(clientId, license, now);
    }

    @Transactional
    public LicenseView disable(long clientId, UUID requestId, long actorUserId) {
        requireBoundClient(clientId);
        LicenseEntity license = licenses.findByClientId(clientId);
        if (alreadyApplied(requestId, clientId, "DISABLE")) return view(clientId, license, now());
        if (license == null) throw new BusinessException(40021, "该机器尚未授予授权");
        LocalDateTime now = now();
        LocalDateTime previous = license.getExpiresAt();
        license.setStatus("DISABLED");
        license.setDisabledAt(now);
        license.setUpdatedAt(now);
        licenses.updateById(license);
        recordOperation(requestId, clientId, actorUserId, "DISABLE", 0, previous, previous, now);
        return view(clientId, license, now);
    }

    @Transactional
    public LicenseView heartbeat(String machineId, long userId, HeartbeatRequest request) {
        ClientEntity client = clients.findByMachineId(machineId);
        if (client == null || !"BOUND".equals(client.getStatus())
                || client.getBoundUserId() == null || client.getBoundUserId() != userId) {
            throw new ClientBindingException();
        }
        LocalDateTime now = now();
        LicenseView result = view(client.getId(), licenses.findByClientId(client.getId()), now);
        LocalDateTime clientTime = reportedTime(request.clientTime());
        Long skew = clientTime == null ? null : Duration.between(clientTime, now).getSeconds();
        heartbeats.record(client.getId(), userId, now, result.status(), result.shouldLock(),
                result.remainingSeconds(), request.sequence(), clientTime, skew,
                skew != null && (skew < -300 || skew > 300), reportedTime(request.offlineSince()));
        clients.markHeartbeat(client.getId(), now);
        return result;
    }

    @Transactional(readOnly = true)
    public boolean canUse(String machineId) {
        ClientEntity client = clients.findByMachineId(machineId);
        LocalDateTime now = now();
        return client != null && "BOUND".equals(client.getStatus())
                && client.getLastHeartbeatAt() != null
                && !client.getLastHeartbeatAt().isAfter(now)
                && Duration.between(client.getLastHeartbeatAt(), now).toMillis() <= OFFLINE_GRACE_SECONDS * 1000
                && view(client.getId(), licenses.findByClientId(client.getId()), now).valid();
    }

    private ClientEntity requireBoundClient(long clientId) {
        ClientEntity client = clients.lockById(clientId);
        if (client == null) throw new BusinessException(40003, "客户端不存在");
        if (!"BOUND".equals(client.getStatus()) || client.getBoundUserId() == null) {
            throw new BusinessException(40023, "请先绑定机器再授予授权");
        }
        return client;
    }

    private boolean alreadyApplied(UUID requestId, long clientId, String action) {
        LicenseOperationEntity operation = operations.selectById(requestId.toString());
        if (operation == null) return false;
        if (operation.getClientId() != clientId || !action.equals(operation.getAction())) {
            throw new BusinessException(40024, "请求编号已用于其他授权操作");
        }
        return true;
    }

    private void recordOperation(UUID requestId, long clientId, long actorUserId, String action, int days,
                                 LocalDateTime previous, LocalDateTime resulting, LocalDateTime now) {
        LicenseOperationEntity operation = new LicenseOperationEntity();
        operation.setRequestId(requestId.toString());
        operation.setClientId(clientId);
        operation.setActorUserId(actorUserId);
        operation.setAction(action);
        operation.setDaysAdded(days);
        operation.setPreviousExpiresAt(previous);
        operation.setResultingExpiresAt(resulting);
        operation.setCreatedAt(now);
        operations.insert(operation);
    }

    private LicenseView view(long clientId, LicenseEntity license, LocalDateTime now) {
        String status;
        long seconds = 0;
        long days = 0;
        boolean valid = false;
        if (license == null) status = "UNGRANTED";
        else if ("DISABLED".equals(license.getStatus())) status = "DISABLED";
        else if (!"ACTIVE".equals(license.getStatus())) status = "INVALID";
        else if (!license.getExpiresAt().isAfter(now)) status = "EXPIRED";
        else {
            long millis = Duration.between(now, license.getExpiresAt()).toMillis();
            seconds = (millis + 999) / 1000;
            days = (seconds + 86399) / 86400;
            valid = true;
            status = seconds <= EXPIRING_SECONDS ? "EXPIRING" : "ACTIVE";
        }
        return new LicenseView(clientId, status, valid, !valid,
                license == null ? 0 : license.getGrantedDaysTotal(), seconds, days,
                license == null ? null : offset(license.getExpiresAt()), offset(now),
                HEARTBEAT_INTERVAL_SECONDS, OFFLINE_GRACE_SECONDS);
    }

    private LocalDateTime now() { return LocalDateTime.now(clock).truncatedTo(ChronoUnit.MILLIS); }
    private OffsetDateTime offset(LocalDateTime value) { return value.atZone(clock.getZone()).toOffsetDateTime(); }
    private LocalDateTime reportedTime(OffsetDateTime value) {
        return value == null ? null : value.atZoneSameInstant(clock.getZone()).toLocalDateTime();
    }
}
