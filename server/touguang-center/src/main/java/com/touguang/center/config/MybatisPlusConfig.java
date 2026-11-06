package com.touguang.center.config;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.Configuration;

@Configuration(proxyBeanMethods = false)
@MapperScan("com.touguang.center.repository.mapper")
public class MybatisPlusConfig {
}
