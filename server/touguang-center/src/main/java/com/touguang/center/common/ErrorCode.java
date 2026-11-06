package com.touguang.center.common;

import org.springframework.http.HttpStatus;

public enum ErrorCode {
    INVALID_REQUEST(40001, HttpStatus.BAD_REQUEST, "请求参数无效"),
    BUSINESS_ERROR(40000, HttpStatus.BAD_REQUEST, "请求未能完成"),
    DEPENDENCY_UNAVAILABLE(50300, HttpStatus.SERVICE_UNAVAILABLE, "依赖服务暂不可用"),
    INTERNAL_ERROR(50000, HttpStatus.INTERNAL_SERVER_ERROR, "服务内部错误，请稍后重试");

    private final int code;
    private final HttpStatus httpStatus;
    private final String message;

    ErrorCode(int code, HttpStatus httpStatus, String message) {
        this.code = code;
        this.httpStatus = httpStatus;
        this.message = message;
    }

    public int code() {
        return code;
    }

    public HttpStatus httpStatus() {
        return httpStatus;
    }

    public String message() {
        return message;
    }
}
