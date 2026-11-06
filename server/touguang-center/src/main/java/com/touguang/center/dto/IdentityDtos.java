package com.touguang.center.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.LocalDateTime;
import java.util.List;

public final class IdentityDtos {
    private IdentityDtos() { }

    public record LoginRequest(@NotBlank String username, @NotBlank String password,
                               @Pattern(regexp = "[a-f0-9]{64}", message = "机器标识必须是 64 位小写十六进制 SHA-256") String machineId) { }
    public record UserView(long id, String username, String displayName, List<String> roles, List<String> permissions) { }
    public record LoginResponse(String token, long expiresInSeconds, UserView user) { }
    public record CreateUserRequest(
            @NotBlank @Pattern(regexp = "[A-Za-z0-9_.-]{3,64}") String username,
            @NotBlank @Size(min = 12, max = 128) String password,
            @NotBlank @Size(max = 100) String displayName,
            @NotEmpty List<String> roles) { }
    public record RegisterClientRequest(
            @NotBlank @Pattern(regexp = "[a-f0-9]{64}", message = "机器标识必须是 64 位小写十六进制 SHA-256") String machineId,
            @NotBlank @Size(max = 100, message = "机器名最多 100 字") String machineName) { }
    public record RegistrationResponse(long id, String status) { }
    public record BindClientRequest(@NotNull Long userId) { }
    public record ClientView(long id, String machineId, String machineName, String status,
                             Long boundUserId, LocalDateTime boundAt, String onlineStatus,
                             LocalDateTime lastHeartbeatAt, LocalDateTime createdAt) { }
}
