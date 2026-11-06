package com.touguang.center.repository.mapper;

import java.time.LocalDateTime;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;

public interface HeartbeatMapper {
    @Insert("""
            INSERT INTO center_heartbeat
              (client_id,user_id,received_at,license_status,should_lock,remaining_seconds,
               client_sequence,client_reported_at,clock_skew_seconds,clock_anomaly,offline_since)
            VALUES
              (#{clientId},#{userId},#{receivedAt},#{status},#{shouldLock},#{remainingSeconds},
               #{sequence},#{clientReportedAt},#{clockSkewSeconds},#{clockAnomaly},#{offlineSince})
            """)
    int record(@Param("clientId") long clientId, @Param("userId") long userId,
               @Param("receivedAt") LocalDateTime receivedAt, @Param("status") String status,
               @Param("shouldLock") boolean shouldLock, @Param("remainingSeconds") long remainingSeconds,
               @Param("sequence") Long sequence, @Param("clientReportedAt") LocalDateTime clientReportedAt,
               @Param("clockSkewSeconds") Long clockSkewSeconds, @Param("clockAnomaly") boolean clockAnomaly,
               @Param("offlineSince") LocalDateTime offlineSince);
}
