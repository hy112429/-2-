package com.touguang.center.dto;

public record HealthStatusDto(String status, String version, Dependency mysql, Dependency redis) {
    public record Dependency(String status, String message) {
    }
}
