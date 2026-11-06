package com.touguang.center.config;

import java.time.Clock;
import java.time.ZoneId;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
public class TimeConfig {
    @Bean
    public Clock centerClock() {
        return Clock.system(ZoneId.of("Asia/Shanghai"));
    }
}
