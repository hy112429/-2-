package com.touguang.center.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.touguang.center.common.ApiResponse;
import com.touguang.center.dto.IdentityDtos.UserView;
import com.touguang.center.service.AuthService;
import com.touguang.center.service.LicenseService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

@Component
public class PermissionInterceptor implements HandlerInterceptor {
    public static final String CURRENT_USER = "center.currentUser";
    public static final String CURRENT_TOKEN = "center.currentToken";
    private final AuthService auth;
    private final LicenseService licenses;
    private final ObjectMapper json;

    public PermissionInterceptor(AuthService auth, LicenseService licenses, ObjectMapper json) {
        this.auth = auth;
        this.licenses = licenses;
        this.json = json;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws IOException {
        if (!(handler instanceof HandlerMethod method)) return deny(response, "接口未授权");
        if (method.hasMethodAnnotation(PublicApi.class)) return true;
        RequirePermission required = method.getMethodAnnotation(RequirePermission.class);
        if (required == null) return deny(response, "接口未配置访问权限");
        String header = request.getHeader("Authorization");
        String token = header != null && header.startsWith("Bearer ") ? header.substring(7).trim() : "";
        UserView user = auth.authenticate(token, request.getHeader("X-Machine-Id"));
        if (user == null) return deny(response, "请先登录或重新登录");
        if (!user.permissions().contains(required.value())) return deny(response, "当前账号没有此操作权限");
        String machineId = request.getHeader("X-Machine-Id");
        boolean exempt = method.hasMethodAnnotation(LicenseExempt.class)
                || method.getBeanType().isAnnotationPresent(LicenseExempt.class);
        if (machineId != null && !machineId.isBlank() && !exempt && !licenses.canUse(machineId)) {
            return deny(response, "机器授权未生效、已停用或已到期");
        }
        request.setAttribute(CURRENT_USER, user);
        request.setAttribute(CURRENT_TOKEN, token);
        return true;
    }

    private boolean deny(HttpServletResponse response, String message) throws IOException {
        response.setStatus(403);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding("UTF-8");
        json.writeValue(response.getWriter(), ApiResponse.failure(40300, message));
        return false;
    }
}
