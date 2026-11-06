package com.touguang.center.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import java.time.OffsetDateTime;
import java.util.UUID;

public final class LicenseDtos {
    private LicenseDtos() { }

    public record DurationRequest(@NotNull UUID requestId, @Min(1) @Max(3650) int days) { }
    public record DisableRequest(@NotNull UUID requestId) { }
    public record HeartbeatRequest(@Min(0) Long sequence, OffsetDateTime clientTime,
                                   OffsetDateTime offlineSince) { }
    public record LicenseView(long clientId, String status, boolean valid, boolean shouldLock,
                              long grantedDaysTotal, long remainingSeconds, long remainingDays,
                              OffsetDateTime expiresAt, OffsetDateTime serverTime,
                              long heartbeatIntervalSeconds, long offlineGraceSeconds) { }
}
