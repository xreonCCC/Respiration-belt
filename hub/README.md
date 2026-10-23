# Hub：树莓派侧数据接收/质检/归档/状态
root  password：halloween
xreon password：123456

## 组成

| 文件 | 作用 | 运行位置 |
|---|---|---|
| `session_scan.py` | 解析一份会话 CSV，输出质量报告（resp_v2 44列/39列/35列） | 两边通用 |
| `hubcheck.py` | 命令行质检工具，逐文件打印 GOOD/IMU_ONLY/... | Mac 或树莓派 |
| `hub_ingest.py` | inbox -> archive/quarantine，并写 `*.quality.json` | 树莓派 |
| `hub_health.py` | `:8000/health` JSON + 网页状态页 | 树莓派 |
| `hub_uplink.py` | 归档目录 -> 目的地（外部盘/挂载的服务器路径），SHA-256 校验+断点续传 | 树莓派 |
| `systemd/` | 开机自启 ingest 定时器和状态页服务 | 树莓派 |

除 `rtc_sync.py` 使用的 `pyserial` 外只依赖 Python 3 标准库；状态页、摄取和上传不需要第三方库。

## RTC 校时

`rtc_sync.py` 通过树莓派已同步的系统时钟向支持该协议的 Node 串口发送
`RTCSET <UTC Unix 秒>`，再等待 Node 输出独立的 DS3231 状态行完成回读校验。
每次运行写入 `state/rtc_sync.jsonl`，最新结果写入
`state/rtc_sync_last.json`。安装依赖并部署定时器和串口触发规则：

```bash
sudo apt install -y python3-serial
sudo cp ~/hub/systemd/hub-rtc-sync.service \
    ~/hub/systemd/hub-rtc-sync.timer \
    ~/hub/systemd/hub-rtc-sync-udev.service /etc/systemd/system/
sudo cp ~/hub/udev/99-respact-rtc.rules /etc/udev/rules.d/
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
# 仅旧版支持 RTCSET/DS3231 的 Node 才启用：
# sudo systemctl enable --now hub-rtc-sync.timer
```

`board_bringup_test` 当前固件尚未实现 `RTCSET` 或 DS3231 状态输出，因此这组
RTC service/udev 文件在最新固件上应保持未安装或未启用；`rtc_sync.py` 只保留为
兼容旧版 RTC 固件的可选工具。串口设备必须由 Node 固件实现 `RTCSET` 命令，并周期性输出
`RTC DS3231 | state=SYNCED | epoch=<seconds>`；否则校时程序会记录失败而不会把
命令当作成功。

## 质量判定规则（依据 ../data/datasetV1.md）

- `GOOD`：双呼吸带各有 >=10 个唯一 UART 帧（按 `*_resp_uart_sample_us` 去重）
- `IMU_ONLY` / `CHEST_BELT_MISSING` / `ABDOMEN_BELT_MISSING` / `BOTH_BELTS_MISSING`
- `SHORT_RECORDING`：<200 行（20 Hz 下约 10 秒）
- `HAS_LARGE_GAP`：`mono_us` 相邻行缺口 >= 5 秒
- `PARSE_FAILED`：没有表头/读不了 -> 进 quarantine

最新 `board_bringup_test` 文件名也已支持：`Rxxxxxxx.CSV` 为测量文件，
`EVENTS.CSV` 为设备事件，`Txxxxxxx.CSV` 为 Node 启动自检文件。启动自检会归档为
`SD_TEST`，不会计入会话数量；含 `schema_version=resp_v2` 的合成数据会标记为
`SYNTHETIC_DATA`。

已知校准样例（用 ../data/ 实测过）：
`LOG013.CSV` 应判 `BOTH_BELTS_MISSING`（IMU 正常但双带失效）。

## 树莓派部署（假设账户 xreon）

```bash
# 从 Mac 上传
scp -r esp32-s3-sleep/hub xreon@192.168.31.219:~/hub

# 在树莓派上
mkdir -p ~/hub/inbox ~/hub/archive ~/hub/quarantine
sudo cp ~/hub/systemd/hub-health.service ~/hub/systemd/hub-ingest.service \
    ~/hub/systemd/hub-ingest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hub-ingest.timer hub-health.service

# 验证
systemctl list-timers | grep hub
curl -s http://127.0.0.1:8000/health
```

## 日常模拟一次 Node 交付

```bash
# Mac：把一份真实 CSV 丢进 inbox（模拟读卡器拷出）
scp "esp32-s3-sleep/../data/压电/REC_20260826_180819_15bpm_small-motion.CSV" \
    xreon@192.168.31.219:hub/inbox/

# 树莓派：手动触发一次 ingest（不等 timer）
systemctl start hub-ingest.service
journalctl -u hub-ingest.service -n 20 --no-pager
```

## 下一步（按优先级）

1. `hub_health.py` 增加 TCP 8765 监听，接 Node `wifi_stream.c` 的实时 JSON
2. ingest 增加 udev 触发：识别 "Sleep Monitor TF Card Export" U 盘后
   `mount -o ro` -> 把挂载点作为 `--inbox` -> `umount`
3. `hub_uplink.py` 目的地换成真实服务器（rsync/rclone）
4. 会话元数据：推动固件侧加 `SESSION_*.json`（被试/部位/固件版本）
