package com.touguang.center.service;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.touguang.center.common.BusinessException;
import com.touguang.center.dto.IdentityDtos.CreateUserRequest;
import com.touguang.center.dto.IdentityDtos.LoginResponse;
import com.touguang.center.dto.IdentityDtos.UserView;
import com.touguang.center.entity.UserEntity;
import com.touguang.center.entity.ClientEntity;
import com.touguang.center.repository.mapper.ClientMapper;
import com.touguang.center.repository.mapper.IdentityMapper;
import com.touguang.center.repository.mapper.UserMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.Base64;
import java.util.HexFormat;
import java.util.LinkedHashSet;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AuthService {
    private static final String SESSION_PREFIX = "tg:center:session:";
    private final UserMapper users;
    private final ClientMapper clients;
    private final IdentityMapper identities;
    private final StringRedisTemplate redis;
    private final PasswordEncoder passwords;
    private final SecureRandom random = new SecureRandom();
    private final long sessionSeconds;

    public AuthService(UserMapper users, ClientMapper clients, IdentityMapper identities, StringRedisTemplate redis,
                       PasswordEncoder passwords, @Value("${CENTER_SESSION_TTL_SECONDS:43200}") long sessionSeconds) {
        this.users = users;
        this.clients = clients;
        this.identities = identities;
        this.redis = redis;
        this.passwords = passwords;
        this.sessionSeconds = Math.max(300, Math.min(sessionSeconds, 86400));
    }

    public LoginResponse login(String username, String password, String machineId) {
        UserEntity user = users.selectOne(new LambdaQueryWrapper<UserEntity>().eq(UserEntity::getUsername, username.trim()));
        if (user == null || !"ACTIVE".equals(user.getStatus()) || !passwords.matches(password, user.getPasswordHash())) {
            throw new InvalidLoginException();
        }
        // 初始管理员可从受控管理端登录完成首次机器绑定。普通客户端必须先登记并由管理员绑定。
        if (machineId == null || machineId.isBlank()) {
            if (!identities.rolesForUser(user.getId()).contains("ADMIN")) throw new ClientBindingException();
            machineId = "-";
        } else if (!isBoundToUser(machineId, user.getId())) {
            throw new ClientBindingException();
        }
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        redis.opsForValue().set(SESSION_PREFIX + sha256(token), user.getId() + ":" + machineId, Duration.ofSeconds(sessionSeconds));
        return new LoginResponse(token, sessionSeconds, view(user));
    }

    public UserView authenticate(String token, String machineId) {
        if (token == null || !token.matches("[A-Za-z0-9_-]{43}")) return null;
        String session = redis.opsForValue().get(SESSION_PREFIX + sha256(token));
        if (session == null) return null;
        String[] parts = session.split(":", 2);
        if (parts.length != 2 || !parts[0].matches("\\d+")) return null;
        long userId = Long.parseLong(parts[0]);
        if (!"-".equals(parts[1]) && (!parts[1].equals(machineId) || !isBoundToUser(parts[1], userId))) return null;
        UserEntity user = users.selectById(userId);
        return user != null && "ACTIVE".equals(user.getStatus()) ? view(user) : null;
    }

    private boolean isBoundToUser(String machineId, long userId) {
        ClientEntity client = clients.findByMachineId(machineId);
        return client != null && "BOUND".equals(client.getStatus())
                && client.getBoundUserId() != null && client.getBoundUserId() == userId;
    }

    public void logout(String token) {
        if (token != null && token.matches("[A-Za-z0-9_-]{43}")) redis.delete(SESSION_PREFIX + sha256(token));
    }

    @Transactional
    public UserView createUser(CreateUserRequest request) {
        if (users.selectCount(new LambdaQueryWrapper<UserEntity>().eq(UserEntity::getUsername, request.username())) > 0) {
            throw new BusinessException(40005, "用户名已存在");
        }
        LinkedHashSet<Long> roleIds = new LinkedHashSet<>();
        for (String key : request.roles()) {
            Long id = identities.roleId(key);
            if (id == null) throw new BusinessException(40006, "未知角色：" + key);
            roleIds.add(id);
        }
        UserEntity user = new UserEntity();
        user.setUsername(request.username());
        user.setDisplayName(request.displayName().trim());
        user.setPasswordHash(passwords.encode(request.password()));
        user.setStatus("ACTIVE");
        users.insert(user);
        for (long id : roleIds) identities.addUserRole(user.getId(), id);
        return view(user);
    }

    private UserView view(UserEntity user) {
        return new UserView(user.getId(), user.getUsername(), user.getDisplayName(),
                identities.rolesForUser(user.getId()), identities.permissionsForUser(user.getId()));
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("系统缺少 SHA-256", impossible);
        }
    }

    public static final class InvalidLoginException extends RuntimeException { }
    public static final class ClientBindingException extends RuntimeException { }
}
