

REQUIRED OS VARS:
```
WG_CONFIG_PATH=/path/to/wg0.conf
LOCAL_RECORDINGS_PATH=/path/to/frigate/recordings
SFTP_HOST=sftp.example.com
SFTP_PORT=22
SFTP_USERNAME=your-username
SFTP_PRIVATE_KEY_PATH=/path/to/your/private_key  # or use password
SFTP_PASSWORD=your-password  # Set if not using key
REMOTE_BASE_PATH=/backup/frigate
DAYS_TO_KEEP=7
BACKUP_SCHEDULE=0 0 * * *  # Cron schedule for backup (default: daily at midnight)
```