package com.touguang.center.repository.mapper;

import java.util.List;
import java.util.Map;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface IdentityMapper {
    @Select("SELECT r.role_key FROM center_role r JOIN center_user_role ur ON ur.role_id=r.id WHERE ur.user_id=#{userId} ORDER BY r.id")
    List<String> rolesForUser(@Param("userId") long userId);

    @Select("SELECT DISTINCT p.permission_key FROM center_permission p JOIN center_role_permission rp ON rp.permission_id=p.id JOIN center_user_role ur ON ur.role_id=rp.role_id WHERE ur.user_id=#{userId} ORDER BY p.permission_key")
    List<String> permissionsForUser(@Param("userId") long userId);

    @Select("SELECT role_key, role_name FROM center_role ORDER BY id")
    List<Map<String, Object>> allRoles();

    @Select("SELECT permission_key, permission_name FROM center_permission ORDER BY id")
    List<Map<String, Object>> allPermissions();

    @Insert("INSERT INTO center_user_role(user_id,role_id) VALUES(#{userId},#{roleId})")
    int addUserRole(@Param("userId") long userId, @Param("roleId") long roleId);

    @Select("SELECT id FROM center_role WHERE role_key=#{roleKey}")
    Long roleId(@Param("roleKey") String roleKey);
}
