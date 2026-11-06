package com.touguang.center.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;

@TableName("center_client")
public class ClientEntity {
    @TableId(type = IdType.AUTO)
    private Long id;
    private String machineId;
    private String machineName;
    private String status;
    private Long boundUserId;
    private LocalDateTime boundAt;
    private String onlineStatus;
    private LocalDateTime lastHeartbeatAt;
    private LocalDateTime createdAt;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getMachineId() { return machineId; }
    public void setMachineId(String machineId) { this.machineId = machineId; }
    public String getMachineName() { return machineName; }
    public void setMachineName(String machineName) { this.machineName = machineName; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public Long getBoundUserId() { return boundUserId; }
    public void setBoundUserId(Long boundUserId) { this.boundUserId = boundUserId; }
    public LocalDateTime getBoundAt() { return boundAt; }
    public void setBoundAt(LocalDateTime boundAt) { this.boundAt = boundAt; }
    public String getOnlineStatus() { return onlineStatus; }
    public void setOnlineStatus(String onlineStatus) { this.onlineStatus = onlineStatus; }
    public LocalDateTime getLastHeartbeatAt() { return lastHeartbeatAt; }
    public void setLastHeartbeatAt(LocalDateTime lastHeartbeatAt) { this.lastHeartbeatAt = lastHeartbeatAt; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public void setCreatedAt(LocalDateTime createdAt) { this.createdAt = createdAt; }
}
