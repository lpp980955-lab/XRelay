"""Reviewed client configuration fragments; never read or write client settings."""
import copy,json,tomllib

SERVER='saxs-waxs-workbench'
PROFILES={
    'generic':dict(name='通用 MCP JSON',format='json',file='xrelay-mcp.json',location='支持本地 stdio MCP 的客户端导入入口',
        url='https://modelcontextprotocol.io/specification/2025-06-18/basic/transports',status='通用模板；具体客户端待验证'),
    'codex':dict(name='Codex',format='toml',file='xrelay-codex.toml',location='用户级 ~/.codex/config.toml 中的 mcp_servers 表',
        url='https://developers.openai.com/codex/mcp/',status='本机原生 app-server 工具调用已验收；桌面聊天和其他环境待验证'),
    'claude':dict(name='Claude Code',format='json',file='xrelay-claude.mcp.json',location='项目根目录 .mcp.json；需在客户端确认项目服务权限',
        url='https://code.claude.com/docs/en/mcp',status='官方格式已核对；原生客户端待测试'),
    'gemini':dict(name='Gemini CLI',format='json',file='xrelay-gemini-settings.json',location='~/.gemini/settings.json 或项目 .gemini/settings.json 的 mcpServers',
        url='https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md',status='官方格式已核对；原生客户端待测试'),
    'workbuddy':dict(name='WorkBuddy',format='json',file='xrelay-workbuddy-mcp.json',location='5.11.3桌面版：连接器 → 自定义连接器 → 添加MCP，编辑 ~/.workbuddy/mcp.json；首次信任由用户确认',
        url='https://open.workbuddy.cn/docs/connector',status='本机5.11.3 + Hy4最小径向积分流程已验收；其他环境与新版EXE待验证'),
    'grok':dict(name='Grok Build（官方 CLI）',format='toml',file='xrelay-grok.toml',location='~/.grok/config.toml 中的 mcp_servers 表；不代表 Grok 网页或其他同名 Agent',
        url='https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/07-mcp-servers.md',status='官方 CLI 格式已核对；原生客户端待测试'),
    'other':dict(name='DeepSeek / Kimi / 豆包等（需明确客户端）',format='json',file='xrelay-other-mcp.json',location='仅适用于实际支持本地 stdio MCP 的桌面或 CLI 客户端；模型 API / 普通网页聊天不等同于客户端',
        url='',status='具体产品和版本未确认；仅生成通用模板，不宣称兼容'),
}


def render_config(entry,profile='generic'):
    settings=copy.deepcopy(entry)
    if profile not in PROFILES:raise ValueError('未知客户端类型')
    if PROFILES[profile]['format']=='toml':
        quote=lambda value:json.dumps(value,ensure_ascii=False)
        lines=[f'[mcp_servers.{quote(SERVER)}]',f'command = {quote(settings["command"])}',
               'args = '+quote(settings['args']),'startup_timeout_sec = 30']
        if settings.get('env'):
            lines+=['',f'[mcp_servers.{quote(SERVER)}.env]']
            lines += [f'{quote(k)} = {quote(v)}' for k,v in settings['env'].items()]
        text='\n'.join(lines)+'\n';tomllib.loads(text);return text
    if profile in ('claude','workbuddy'):settings['type']='stdio'
    if profile in ('gemini','workbuddy'):settings['timeout']=30000
    return json.dumps({'mcpServers':{SERVER:settings}},ensure_ascii=False,indent=2)+'\n'


def merge_preview(existing,fragment,profile):
    """Preserve unrelated settings; a conflicting service requires manual review."""
    if PROFILES[profile]['format']=='toml':
        before=tomllib.loads(existing);addition=tomllib.loads(fragment)
        current=before.get('mcp_servers',{}).get(SERVER)
        wanted=addition['mcp_servers'][SERVER]
        if current is not None:
            if any(current.get(key)!=value for key,value in wanted.items()):raise ValueError('同名服务已有不同配置；请人工核对，不自动覆盖。')
            return existing
        combined=existing.rstrip()+'\n\n'+fragment
        tomllib.loads(combined)
        return combined
    before=json.loads(existing or '{}');addition=json.loads(fragment)
    if not isinstance(before,dict):raise ValueError('已有配置必须是 JSON 对象')
    result=copy.deepcopy(before);servers=result.setdefault('mcpServers',{})
    if not isinstance(servers,dict):raise ValueError('mcpServers 必须是对象')
    wanted=addition['mcpServers'][SERVER];current=servers.get(SERVER)
    if current is not None and current!=wanted:raise ValueError('同名服务已有不同配置；请人工核对，不自动覆盖。')
    servers[SERVER]=wanted
    return json.dumps(result,ensure_ascii=False,indent=2)+'\n'


def instructions(profile,language='zh'):
    p=PROFILES[profile]
    if language=='en':
        locations={
            'generic':'Import into a client that supports local MCP stdio.',
            'codex':'Merge into mcp_servers in ~/.codex/config.toml.',
            'claude':'Merge into the project .mcp.json; approve the project server in Claude Code.',
            'gemini':'Merge mcpServers into ~/.gemini/settings.json or project .gemini/settings.json.',
            'workbuddy':'WorkBuddy 5.11.3: Connectors > Custom connectors > Add MCP, ~/.workbuddy/mcp.json. First-use trust is confirmed by the user. The local 5.11.3 + Hy4 minimal radial workflow passed with the source Agent; other environments and new packaged builds remain unverified.',
            'grok':'Merge into mcp_servers in ~/.grok/config.toml for the official Grok Build CLI.',
            'other':'Exact client not specified. Model APIs and ordinary web chats are not MCP clients.',
        }
        return (locations[profile]+'\n\nClient-session connection remains UNVERIFIED.\n\n'
            '1. Choose the input and output folders, then generate the configuration.\n'
            '2. Back up the existing client configuration and merge this service entry. Do not replace the entire settings file with a fragment.\n'
            '3. Reload or restart the client and approve its MCP tool permissions.\n'
            '4. Call get_workflow_guide, list_files, inspect_source and inspect_image first.\n'
            '5. Confirm experimental parameters before calculations; poll get_job for progress.\n\n'
            'The local service test only initializes a separate process and lists tools. It does not read experimental frames, run a model or prove the client is connected.\n'
            'XRelay does not install clients, store model keys or upload data. The client controls its own model and network use.\n\n'
            'Official reference reviewed 2026-11-24: '+(p['url'] or 'Pending exact client product and version.'))
    return (f'{p["name"]}\n{p["status"]}\n\n配置位置／方式：{p["location"]}\n\n'
        '1. 选择本次允许读取的数据目录和输出目录，生成配置。\n'
        '2. 复制该服务条目，手动合并到客户端配置；不要用片段覆盖整个已有配置文件。\n'
        '3. 在客户端按其要求重启／重载并确认 MCP 工具权限。\n'
        '4. 先调用 get_workflow_guide、list_files、inspect_source、inspect_image。\n'
        '5. 明确实验参数后再请求计算，使用 get_job 查询后台任务。\n\n'
        '“测试本地服务”只检查独立进程的初始化和工具发现，不读取实验帧、不运行模型、也不证明客户端会话已连接。\n'
        'XRelay 不上传文件、不保存模型密钥、不自动安装客户端；客户端如何使用工具结果由其自身设置决定。\n\n'
        '官方依据（核对日期：2026-11-24）：'+(p['url'] or '待提供具体客户端产品、版本和官方文档。'))
