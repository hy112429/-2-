package com.touguang.center.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

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
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class LicenseServiceTest {
    private static final ZoneId ZONE = ZoneId.of("Asia/Shanghai");
    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-11-06T08:00:00Z"), ZONE);
    private static final LocalDateTime NOW = LocalDateTime.of(2026, 11, 6, 16, 0);

    @Mock ClientMapper clients;
    @Mock LicenseMapper licenses;
    @Mock LicenseOperationMapper operations;
    @Mock HeartbeatMapper heartbeats;
    LicenseService service;

    @BeforeEach
    void setUp() {
        service = new LicenseService(clients, licenses, operations, heartbeats, CLOCK);
    }

    @Test
    void grantUsesServerTimeAndRequiresBoundMachine() {
        when(clients.lockById(2)).thenReturn(boundClient());
        UUID requestId = UUID.randomUUID();
        LicenseView result = service.grant(2, 5, requestId, 3);
        assertEquals("ACTIVE", result.status());
        assertEquals(5, result.remainingDays());
        assertEquals(NOW.plusDays(5).atZone(ZONE).toOffsetDateTime(), result.expiresAt());
        ArgumentCaptor<LicenseEntity> saved = ArgumentCaptor.forClass(LicenseEntity.class);
        verify(licenses).insert(saved.capture());
        assertEquals(NOW.plusDays(5), saved.getValue().getExpiresAt());
        verify(operations).insert(any(LicenseOperationEntity.class));
    }

    @Test
    void renewalFromExistingExpiryIsIdempotent() {
        when(clients.lockById(2)).thenReturn(boundClient());
        LicenseEntity license = activeLicense(NOW.plusDays(2));
        when(licenses.findByClientId(2)).thenReturn(license);
        UUID requestId = UUID.randomUUID();
        LicenseView first = service.renew(2, 3, requestId, 3);
        LicenseOperationEntity prior = new LicenseOperationEntity();
        prior.setRequestId(requestId.toString());
        prior.setClientId(2L);
        prior.setAction("RENEW");
        when(operations.selectById(requestId.toString())).thenReturn(prior);
        LicenseView repeated = service.renew(2, 3, requestId, 3);
        assertEquals(NOW.plusDays(5).atZone(ZONE).toOffsetDateTime(), first.expiresAt());
        assertEquals(first.expiresAt(), repeated.expiresAt());
        verify(licenses).updateById(license);
        verify(operations).insert(any(LicenseOperationEntity.class));
    }

    @Test
    void expiredAuthorizationLocksAndHeartbeatIsAudited() {
        ClientEntity client = boundClient();
        when(clients.findByMachineId("machine")).thenReturn(client);
        when(licenses.findByClientId(2)).thenReturn(activeLicense(NOW.minusSeconds(1)));
        LicenseView result = service.heartbeat("machine", 3,
                new HeartbeatRequest(4L, OffsetDateTime.parse("2035-01-01T00:00:00+08:00"), null));
        assertEquals("EXPIRED", result.status());
        assertTrue(result.shouldLock());
        assertEquals(0, result.remainingSeconds());
        verify(heartbeats).record(eq(2L), eq(3L), eq(NOW), eq("EXPIRED"),
                eq(true), eq(0L), eq(4L), any(LocalDateTime.class), any(Long.class),
                eq(true), eq(null));
        verify(clients).markHeartbeat(2, NOW);
    }

    @Test
    void stoppedHeartbeatCannotUseProtectedRoutesAfterTenMinutes() {
        ClientEntity client = boundClient();
        client.setLastHeartbeatAt(NOW.minusSeconds(601));
        when(clients.findByMachineId("machine")).thenReturn(client);
        assertFalse(service.canUse("machine"));
    }

    @Test
    void activeHeartbeatAllowsProtectedRoutesWithinTenMinutes() {
        ClientEntity client = boundClient();
        client.setLastHeartbeatAt(NOW.minusSeconds(600));
        when(clients.findByMachineId("machine")).thenReturn(client);
        when(licenses.findByClientId(2)).thenReturn(activeLicense(NOW.plusDays(1)));
        assertTrue(service.canUse("machine"));
    }

    @Test
    void disabledAuthorizationCannotUseProtectedRoutes() {
        ClientEntity client = boundClient();
        client.setLastHeartbeatAt(NOW);
        LicenseEntity license = activeLicense(NOW.plusDays(1));
        license.setStatus("DISABLED");
        when(clients.findByMachineId("machine")).thenReturn(client);
        when(licenses.findByClientId(2)).thenReturn(license);
        assertFalse(service.canUse("machine"));
    }

    @Test
    void unknownStoredStatusFailsClosed() {
        ClientEntity client = boundClient();
        client.setLastHeartbeatAt(NOW);
        LicenseEntity license = activeLicense(NOW.plusDays(1));
        license.setStatus("UNKNOWN");
        when(clients.findByMachineId("machine")).thenReturn(client);
        when(licenses.findByClientId(2)).thenReturn(license);
        assertFalse(service.canUse("machine"));
    }

    private static ClientEntity boundClient() {
        ClientEntity client = new ClientEntity();
        client.setId(2L);
        client.setMachineId("machine");
        client.setStatus("BOUND");
        client.setBoundUserId(3L);
        return client;
    }

    private static LicenseEntity activeLicense(LocalDateTime expiry) {
        LicenseEntity license = new LicenseEntity();
        license.setId(7L);
        license.setClientId(2L);
        license.setStatus("ACTIVE");
        license.setExpiresAt(expiry);
        license.setGrantedDaysTotal(10L);
        return license;
    }
}
