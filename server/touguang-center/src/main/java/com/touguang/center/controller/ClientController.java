package com.touguang.center.controller;

import com.touguang.center.common.ApiResponse;
import com.touguang.center.config.PublicApi;
import com.touguang.center.config.RequirePermission;
import com.touguang.center.dto.IdentityDtos.BindClientRequest;
import com.touguang.center.dto.IdentityDtos.ClientView;
import com.touguang.center.dto.IdentityDtos.RegisterClientRequest;
import com.touguang.center.dto.IdentityDtos.RegistrationResponse;
import com.touguang.center.service.ClientService;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/clients")
public class ClientController {
    private final ClientService clients;

    public ClientController(ClientService clients) { this.clients = clients; }

    @PostMapping("/register")
    @PublicApi
    public ApiResponse<RegistrationResponse> register(@Valid @RequestBody RegisterClientRequest request) {
        return ApiResponse.success(clients.register(request.machineId(), request.machineName()));
    }

    @GetMapping
    @RequirePermission("client:read")
    public ApiResponse<List<ClientView>> list() {
        return ApiResponse.success(clients.list());
    }

    @PostMapping("/{id}/bind")
    @RequirePermission("client:bind")
    public ApiResponse<ClientView> bind(@PathVariable long id, @Valid @RequestBody BindClientRequest request) {
        return ApiResponse.success(clients.bind(id, request.userId()));
    }
}
