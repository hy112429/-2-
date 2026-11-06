package com.touguang.center.controller;

import com.touguang.center.common.ApiResponse;
import com.touguang.center.config.PermissionInterceptor;
import com.touguang.center.config.LicenseExempt;
import com.touguang.center.config.PublicApi;
import com.touguang.center.config.RequirePermission;
import com.touguang.center.dto.IdentityDtos.LoginRequest;
import com.touguang.center.dto.IdentityDtos.LoginResponse;
import com.touguang.center.dto.IdentityDtos.CreateUserRequest;
import com.touguang.center.dto.IdentityDtos.UserView;
import com.touguang.center.service.AuthService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@LicenseExempt
@RequestMapping("/api")
public class AuthController {
    private final AuthService auth;

    public AuthController(AuthService auth) { this.auth = auth; }

    @PostMapping("/auth/login")
    @PublicApi
    public ApiResponse<LoginResponse> login(@Valid @RequestBody LoginRequest request) {
        return ApiResponse.success(auth.login(request.username(), request.password(), request.machineId()));
    }

    @PostMapping("/auth/logout")
    @RequirePermission("user:me")
    public ApiResponse<Void> logout(HttpServletRequest request) {
        auth.logout((String) request.getAttribute(PermissionInterceptor.CURRENT_TOKEN));
        return ApiResponse.success(null);
    }

    @GetMapping("/users/me")
    @RequirePermission("user:me")
    public ApiResponse<UserView> me(HttpServletRequest request) {
        return ApiResponse.success((UserView) request.getAttribute(PermissionInterceptor.CURRENT_USER));
    }

    @PostMapping("/users")
    @RequirePermission("user:create")
    public ApiResponse<UserView> createUser(@Valid @RequestBody CreateUserRequest request) {
        return ApiResponse.success(auth.createUser(request));
    }
}
