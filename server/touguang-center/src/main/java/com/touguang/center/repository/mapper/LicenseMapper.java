package com.touguang.center.repository.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.touguang.center.entity.LicenseEntity;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface LicenseMapper extends BaseMapper<LicenseEntity> {
    @Select("SELECT * FROM center_license WHERE client_id=#{clientId}")
    LicenseEntity findByClientId(@Param("clientId") long clientId);
}
