package com.touguang.center.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.touguang.center.controller.LicenseController;
import com.touguang.center.dto.IdentityDtos.UserView;
import com.touguang.center.dto.LicenseDtos.HeartbeatRequest;
import com.touguang.center.service.AuthService;
import com.touguang.center.service.LicenseService;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.web.method.HandlerMethod;

@ExtendWith(MockitoExtension.class)
class PermissionInterceptorLicenseTest {
    @Mock AuthService auth;
    @Mock LicenseService licenses;
    PermissionInterceptor interceptor;
    MockHttpServletRequest request;
    MockHttpServletResponse response;

    @BeforeEach
    void setUp() {
        interceptor = new PermissionInterceptor(auth, licenses, new ObjectMapper());
        request = new MockHttpServletRequest();
        request.addHeader("Authorization", "Bearer token");
        request.addHeader("X-Machine-Id", "machine");
        response = new MockHttpServletResponse();
    }

    @Test
    void businessRouteIsDeniedWhenHeartbeatIsStaleOrLicenseInvalid() throws Exception {
        when(auth.authenticate("token", "machine"))
                .thenReturn(new UserView(3, "user", "用户", List.of("OPERATOR"), List.of("user:me")));
        when(licenses.canUse("machine")).thenReturn(false);
        HandlerMethod route = new HandlerMethod(new BusinessRoute(),
                BusinessRoute.class.getMethod("run"));
        assertFalse(interceptor.preHandle(request, response, route));
        assertEquals(403, response.getStatus());
        assertTrue(response.getContentAsString().contains("授权"));
        verify(licenses).canUse("machine");
    }

    @Test
    void heartbeatRemainsReachableSoServerCanIssueLockDecision() throws Exception {
        when(auth.authenticate("token", "machine"))
                .thenReturn(new UserView(3, "user", "用户", List.of("OPERATOR"), List.of("user:me")));
        HandlerMethod route = new HandlerMethod(new LicenseController(licenses),
                LicenseController.class.getMethod("heartbeat", HeartbeatRequest.class, HttpServletRequest.class));
        assertTrue(interceptor.preHandle(request, response, route));
    }

    static class BusinessRoute {
        @RequirePermission("user:me")
        public void run() { }
    }
}
