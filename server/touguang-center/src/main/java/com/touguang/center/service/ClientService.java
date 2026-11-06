package com.touguang.center.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.touguang.center.common.BusinessException;
import com.touguang.center.dto.IdentityDtos.ClientView;
import com.touguang.center.dto.IdentityDtos.RegistrationResponse;
import com.touguang.center.entity.ClientEntity;
import com.touguang.center.entity.UserEntity;
import com.touguang.center.repository.mapper.ClientMapper;
import com.touguang.center.repository.mapper.UserMapper;
import java.time.Clock;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ClientService {
    private final ClientMapper clients;
    private final UserMapper users;
    private final Clock clock;

    public ClientService(ClientMapper clients, UserMapper users, Clock clock) {
        this.clients = clients;
        this.users = users;
        this.clock = clock;
    }

    @Transactional
    public RegistrationResponse register(String machineId, String machineName) {
        clients.register(machineId, machineName.trim());
        ClientEntity client = clients.findByMachineId(machineId);
        return new RegistrationResponse(client.getId(), client.getStatus());
    }

    public List<ClientView> list() {
        return clients.selectList(new LambdaQueryWrapper<ClientEntity>().orderByDesc(ClientEntity::getId))
                .stream().map(this::view).toList();
    }

    @Transactional
    public ClientView bind(long clientId, long userId) {
        UserEntity user = users.selectById(userId);
        if (user == null || !"ACTIVE".equals(user.getStatus())) {
            throw new BusinessException(40002, "绑定用户不存在或已停用");
        }
        ClientEntity client = clients.selectById(clientId);
        if (client == null) throw new BusinessException(40003, "客户端不存在");
        if (client.getBoundUserId() != null && client.getBoundUserId() != userId) {
            throw new BusinessException(40004, "客户端已绑定其他用户，不能直接改绑");
        }
        if (clients.bind(clientId, userId) != 1) throw new BusinessException(40004, "客户端绑定状态已改变，请刷新后重试");
        return view(clients.selectById(clientId));
    }

    private ClientView view(ClientEntity client) {
        String onlineStatus = client.getLastHeartbeatAt() == null ? "UNKNOWN"
                : Duration.between(client.getLastHeartbeatAt(), LocalDateTime.now(clock)).getSeconds() > 180
                    ? "OFFLINE" : "ONLINE";
        return new ClientView(client.getId(), client.getMachineId(), client.getMachineName(),
                client.getStatus(), client.getBoundUserId(), client.getBoundAt(),
                onlineStatus, client.getLastHeartbeatAt(), client.getCreatedAt());
    }
}
