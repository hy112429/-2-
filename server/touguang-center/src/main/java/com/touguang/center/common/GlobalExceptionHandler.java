package com.touguang.center.common;

import jakarta.validation.ConstraintViolationException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.BindException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.http.converter.HttpMessageNotReadableException;
import com.touguang.center.service.AuthService.InvalidLoginException;
import com.touguang.center.service.AuthService.ClientBindingException;

@RestControllerAdvice
public class GlobalExceptionHandler {
    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(InvalidLoginException.class)
    public ResponseEntity<ApiResponse<Void>> handleInvalidLogin(InvalidLoginException exception) {
        return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                .body(ApiResponse.failure(40100, "用户名或密码错误"));
    }

    @ExceptionHandler(ClientBindingException.class)
    public ResponseEntity<ApiResponse<Void>> handleClientBinding(ClientBindingException exception) {
        return ResponseEntity.status(HttpStatus.FORBIDDEN)
                .body(ApiResponse.failure(40301, "此机器尚未绑定当前账号，请联系管理员确认绑定"));
    }

    @ExceptionHandler(BusinessException.class)
    public ResponseEntity<ApiResponse<Void>> handleBusiness(BusinessException exception) {
        return ResponseEntity.badRequest()
                .body(ApiResponse.failure(exception.getCode(), exception.getMessage()));
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, BindException.class})
    public ResponseEntity<ApiResponse<Void>> handleValidation(Exception exception) {
        String message = "请求参数不正确";
        if (exception instanceof MethodArgumentNotValidException invalid && invalid.getBindingResult().getFieldError() != null) {
            message = invalid.getBindingResult().getFieldError().getDefaultMessage();
        } else if (exception instanceof BindException invalid && invalid.getBindingResult().getFieldError() != null) {
            message = invalid.getBindingResult().getFieldError().getDefaultMessage();
        }
        return ResponseEntity.badRequest().body(ApiResponse.failure(40001, message));
    }

    @ExceptionHandler({ConstraintViolationException.class, MethodArgumentTypeMismatchException.class, HttpMessageNotReadableException.class})
    public ResponseEntity<ApiResponse<Void>> handleInvalidRequest(Exception exception) {
        return ResponseEntity.badRequest().body(ApiResponse.failure(40001, "请求参数格式不正确"));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleUnexpected(Exception exception) {
        log.error("服务处理请求时发生未预期异常", exception);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(ApiResponse.failure(50000, "服务暂时不可用，请稍后重试"));
    }
}
