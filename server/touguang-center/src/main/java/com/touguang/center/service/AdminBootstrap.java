package com.touguang.center.service;

import com.touguang.center.entity.UserEntity;
import com.touguang.center.repository.mapper.IdentityMapper;
import com.touguang.center.repository.mapper.UserMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

@Component
public class AdminBootstrap implements ApplicationRunner {
    private static final Logger log = LoggerFactory.getLogger(AdminBootstrap.class);
    private final UserMapper users;
    private final IdentityMapper identities;
    private final PasswordEncoder passwords;
    private final String username;
    private final String password;

    public AdminBootstrap(UserMapper users, IdentityMapper identities, PasswordEncoder passwords,
                          @Value("${CENTER_BOOTSTRAP_ADMIN_USERNAME:}") String username,
                          @Value("${CENTER_BOOTSTRAP_ADMIN_PASSWORD:}") String password) {
        this.users = users;
        this.identities = identities;
        this.passwords = passwords;
        this.username = username;
        this.password = password;
    }

    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        if (users.selectCount(null) != 0) return;
        if (username.isBlank() || password.isBlank()) {
            log.warn("尚无管理员：首次启动前请设置 CENTER_BOOTSTRAP_ADMIN_USERNAME 与 CENTER_BOOTSTRAP_ADMIN_PASSWORD");
            return;
        }
        if (!username.matches("[A-Za-z0-9_.-]{3,64}") || password.length() < 12) {
            throw new IllegalStateException("初始管理员用户名格式不正确，或密码不足 12 位");
        }
        UserEntity admin = new UserEntity();
        admin.setUsername(username);
        admin.setDisplayName("初始管理员");
        admin.setPasswordHash(passwords.encode(password));
        admin.setStatus("ACTIVE");
        users.insert(admin);
        identities.addUserRole(admin.getId(), 1);
        log.info("已创建初始管理员；后续启动不会重置其密码");
    }
}
