package com.touguang.center.controller;

import com.touguang.center.common.ApiResponse;
import com.touguang.center.common.ErrorCode;
import com.touguang.center.config.PublicApi;
import com.touguang.center.dto.HealthStatusDto;
import com.touguang.center.service.HealthService;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController {
    private final HealthService healthService;

    public HealthController(HealthService healthService) {
        this.healthService = healthService;
    }

    @GetMapping("/api/health")
    @PublicApi
    public ResponseEntity<ApiResponse<HealthStatusDto>> health() {
        HealthStatusDto status = healthService.inspect();
        if ("UP".equals(status.status())) {
            return ResponseEntity.ok(ApiResponse.success(status));
        }
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ApiResponse.failure(
                        ErrorCode.DEPENDENCY_UNAVAILABLE.code(),
                        "依赖服务连接异常，请检查 MySQL 与 Redis。",
                        status));
    }
}
