package com.touguang.center.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration(proxyBeanMethods = false)
public class PermissionWebConfig implements WebMvcConfigurer {
    private final PermissionInterceptor permissions;

    public PermissionWebConfig(PermissionInterceptor permissions) {
        this.permissions = permissions;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(permissions).addPathPatterns("/api/**");
    }
}
