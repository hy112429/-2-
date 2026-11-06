package com.touguang.center.entity;

import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;

@TableName("center_license_operation")
public class LicenseOperationEntity {
    @TableId
    private String requestId;
    private Long clientId;
    private Long actorUserId;
    private String action;
    private Integer daysAdded;
    private LocalDateTime previousExpiresAt;
    private LocalDateTime resultingExpiresAt;
    private LocalDateTime createdAt;

    public String getRequestId() { return requestId; }
    public void setRequestId(String requestId) { this.requestId = requestId; }
    public Long getClientId() { return clientId; }
    public void setClientId(Long clientId) { this.clientId = clientId; }
    public Long getActorUserId() { return actorUserId; }
    public void setActorUserId(Long actorUserId) { this.actorUserId = actorUserId; }
    public String getAction() { return action; }
    public void setAction(String action) { this.action = action; }
    public Integer getDaysAdded() { return daysAdded; }
    public void setDaysAdded(Integer daysAdded) { this.daysAdded = daysAdded; }
    public LocalDateTime getPreviousExpiresAt() { return previousExpiresAt; }
    public void setPreviousExpiresAt(LocalDateTime previousExpiresAt) { this.previousExpiresAt = previousExpiresAt; }
    public LocalDateTime getResultingExpiresAt() { return resultingExpiresAt; }
    public void setResultingExpiresAt(LocalDateTime resultingExpiresAt) { this.resultingExpiresAt = resultingExpiresAt; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
}
