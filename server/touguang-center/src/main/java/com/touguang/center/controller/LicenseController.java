package com.touguang.center.controller;

import com.touguang.center.common.ApiResponse;
import com.touguang.center.config.LicenseExempt;
import com.touguang.center.config.PermissionInterceptor;
import com.touguang.center.config.RequirePermission;
import com.touguang.center.dto.IdentityDtos.UserView;
import com.touguang.center.dto.LicenseDtos.DisableRequest;
import com.touguang.center.dto.LicenseDtos.DurationRequest;
import com.touguang.center.dto.LicenseDtos.HeartbeatRequest;
import com.touguang.center.dto.LicenseDtos.LicenseView;
import com.touguang.center.service.LicenseService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@LicenseExempt
@RequestMapping("/api/clients")
public class LicenseController {
    private final LicenseService licenses;

    public LicenseController(LicenseService licenses) { this.licenses = licenses; }

    @GetMapping("/{id}/license")
    @RequirePermission("license:read")
    public ApiResponse<LicenseView> get(@PathVariable long id) {
        return ApiResponse.success(licenses.get(id));
    }

    @PostMapping("/{id}/license/grant")
    @RequirePermission("license:write")
    public ApiResponse<LicenseView> grant(@PathVariable long id, @Valid @RequestBody DurationRequest body,
                                         HttpServletRequest request) {
        return ApiResponse.success(licenses.grant(id, body.days(), body.requestId(), currentUser(request).id()));
    }

    @PostMapping("/{id}/license/renew")
    @RequirePermission("license:write")
    public ApiResponse<LicenseView> renew(@PathVariable long id, @Valid @RequestBody DurationRequest body,
                                         HttpServletRequest request) {
        return ApiResponse.success(licenses.renew(id, body.days(), body.requestId(), currentUser(request).id()));
    }

    @PostMapping("/{id}/license/disable")
    @RequirePermission("license:write")
    public ApiResponse<LicenseView> disable(@PathVariable long id, @Valid @RequestBody DisableRequest body,
                                           HttpServletRequest request) {
        return ApiResponse.success(licenses.disable(id, body.requestId(), currentUser(request).id()));
    }

    @PostMapping("/heartbeat")
    @RequirePermission("user:me")
    public ApiResponse<LicenseView> heartbeat(@Valid @RequestBody HeartbeatRequest body,
                                              HttpServletRequest request) {
        return ApiResponse.success(licenses.heartbeat(request.getHeader("X-Machine-Id"),
                currentUser(request).id(), body));
    }

    private UserView currentUser(HttpServletRequest request) {
        return (UserView) request.getAttribute(PermissionInterceptor.CURRENT_USER);
    }
}
