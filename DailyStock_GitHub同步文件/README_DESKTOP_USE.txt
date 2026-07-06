DailyStock 桌面版使用说明

入口：
- 双击桌面：C:\Users\stays\Desktop\DailyStock_控制面板.lnk
- 控制面板文件：C:\Users\stays\Desktop\daily_stock_analysis\daily_stock_control_panel.py

推荐用法：
1. 收盘后打开控制面板。
2. 点“今日重点筛选”。
3. 程序会先批量读取自选股行情和板块热度，生成“今日重点看的票”。
4. 只对入选的少数个股运行 AI 分析并按规则推送；普通观察股只进入报告，避免刷屏。
5. 点“打开报告”查看完整报告。

当前本地已就绪：
- 自选股：66 只
- 通知：钉钉已配置
- 行情：YunAI 已配置
- AI：DeepSeek 已配置，模型 deepseek/deepseek-chat
- 自动回溯：已关闭，避免复盘任务被拖慢
- 推送策略：SINGLE_STOCK_NOTIFY_FILTER=important

核心按钮：
- 今日重点筛选：收盘复盘推荐入口，先筛选再分析。
- 运行大盘复盘：只看大盘，不跑个股。
- 运行个股分析：手动跑完整自选股，耗时较长。
- 测试单股推送：用测试股票发送真实钉钉推送。
- 测试行情：检查 YunAI 是否能返回该股票行情。

本地与 GitHub 的区别：
- 本地控制面板只能在电脑开机时运行。
- 电脑关机也要自动推送，必须使用 GitHub Actions 或一台一直在线的服务器。
- 已新增 GitHub Actions 工作流：.github\workflows\01-daily-focus-review.yml
- 该工作流默认北京时间工作日 18:15 运行“收盘重点筛选”。

推送规则：
- 先发一条“今日重点筛选摘要”，包含热点板块和重点个股。
- 买入 / 加仓 / 减仓 / 卖出 / 预警 / 规避：单股单独推送。
- 评分 >= 65：单股单独推送。
- 普通观望：不单独推送，只进入本地报告。

主要文件：
- 快速筛选脚本：C:\Users\stays\Desktop\daily_stock_analysis\scripts\daily_focus_review.py
- 自选清单：C:\Users\stays\Desktop\daily_stock_analysis\watchlist_stocks.csv
- 报告目录：C:\Users\stays\Desktop\daily_stock_analysis\reports
- 日志目录：C:\Users\stays\Desktop\daily_stock_analysis\logs
- Logo：C:\Users\stays\Desktop\daily_stock_analysis\assets\daily_stock_logo.ico

本地配置项：
- BACKTEST_ENABLED=false
- AGENT_SKILL_AUTOWEIGHT=false
- FOCUS_MAX_STOCKS=8
- FOCUS_TOP_THEMES=5
- FOCUS_MIN_CHANGE_PCT=2.0
- FOCUS_MIN_ABS_CHANGE_PCT=3.0
- FOCUS_MIN_VOLUME_RATIO=1.5
- FOCUS_REQUIRE_FRESH=true

GitHub Actions 云端需要同步的配置：
- Secrets：DINGTALK_WEBHOOK_URL、DINGTALK_SECRET、YUNAI_AUTHORIZATION、DEEPSEEK_API_KEY
- Variables：STOCK_LIST、SINGLE_STOCK_NOTIFY=true、SINGLE_STOCK_NOTIFY_FILTER=important、LITELLM_MODEL=deepseek/deepseek-chat、ANALYSIS_TIMEOUT_MINUTES=60

说明：
- 桌面快捷方式使用 pythonw.exe，不会弹出多余控制台窗口。
- 不要把 .env 或任何 API Key 提交到公开仓库。
