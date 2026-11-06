package com.touguang.center.repository.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.touguang.center.entity.ClientEntity;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

public interface ClientMapper extends BaseMapper<ClientEntity> {
    @Insert("INSERT INTO center_client(machine_id,machine_name,status,online_status) VALUES(#{machineId},#{machineName},'PENDING','UNKNOWN') ON DUPLICATE KEY UPDATE machine_name=VALUES(machine_name)")
    int register(@Param("machineId") String machineId, @Param("machineName") String machineName);

    @Select("SELECT * FROM center_client WHERE machine_id=#{machineId}")
    ClientEntity findByMachineId(@Param("machineId") String machineId);

    @Update("UPDATE center_client SET status='BOUND', bound_user_id=#{userId}, bound_at=COALESCE(bound_at,CURRENT_TIMESTAMP(3)) WHERE id=#{clientId} AND (bound_user_id IS NULL OR bound_user_id=#{userId})")
    int bind(@Param("clientId") long clientId, @Param("userId") long userId);
}
