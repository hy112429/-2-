package com.touguang.center.service;

import com.touguang.center.dto.HealthStatusDto;
import org.springframework.boot.info.BuildProperties;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.RedisCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class HealthService {
    private final JdbcTemplate jdbcTemplate;
    private final StringRedisTemplate stringRedisTemplate;
    private final ObjectProvider<BuildProperties> buildProperties;

    public HealthService(
            JdbcTemplate jdbcTemplate,
            StringRedisTemplate stringRedisTemplate,
            ObjectProvider<BuildProperties> buildProperties) {
        this.jdbcTemplate = jdbcTemplate;
        this.stringRedisTemplate = stringRedisTemplate;
        this.buildProperties = buildProperties;
    }

    public HealthStatusDto inspect() {
        HealthStatusDto.Dependency mysql = checkMysql();
        HealthStatusDto.Dependency redis = checkRedis();
        String overall = "UP".equals(mysql.status()) && "UP".equals(redis.status()) ? "UP" : "DOWN";
        BuildProperties build = buildProperties.getIfAvailable();
        String version = build == null ? "未知" : build.getVersion();
        return new HealthStatusDto(overall, version, mysql, redis);
    }

    private HealthStatusDto.Dependency checkMysql() {
        try {
            Integer result = jdbcTemplate.queryForObject("SELECT 1", Integer.class);
            return result != null && result == 1
                    ? new HealthStatusDto.Dependency("UP", "连接正常")
                    : new HealthStatusDto.Dependency("DOWN", "MySQL 检查未通过");
        } catch (RuntimeException exception) {
            return new HealthStatusDto.Dependency("DOWN", "MySQL 暂不可用");
        }
    }

    private HealthStatusDto.Dependency checkRedis() {
        try {
            String response = stringRedisTemplate.execute((RedisCallback<String>) connection -> connection.ping());
            return "PONG".equalsIgnoreCase(response)
                    ? new HealthStatusDto.Dependency("UP", "连接正常")
                    : new HealthStatusDto.Dependency("DOWN", "Redis 检查未通过");
        } catch (RuntimeException exception) {
            return new HealthStatusDto.Dependency("DOWN", "Redis 暂不可用");
        }
    }
}
