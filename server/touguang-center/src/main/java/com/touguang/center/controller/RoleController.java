package com.touguang.center.controller;

import com.touguang.center.common.ApiResponse;
import com.touguang.center.config.LicenseExempt;
import com.touguang.center.config.RequirePermission;
import com.touguang.center.repository.mapper.IdentityMapper;
import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@LicenseExempt
@RequestMapping("/api")
public class RoleController {
    private final IdentityMapper identities;

    public RoleController(IdentityMapper identities) { this.identities = identities; }

    @GetMapping("/roles")
    @RequirePermission("role:read")
    public ApiResponse<List<Map<String, Object>>> roles() {
        return ApiResponse.success(identities.allRoles());
    }

    @GetMapping("/permissions")
    @RequirePermission("permission:read")
    public ApiResponse<List<Map<String, Object>>> permissions() {
        return ApiResponse.success(identities.allPermissions());
    }
}
