import logging
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta
import sys

# ANSI 转义序列
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    RESET = "\033[0m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

# 基础配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
BASE_URL = "https://pipe-network-backend.pipecanary.workers.dev/api"

# 时间间隔配置
HEARTBEAT_INTERVAL = 300  # 5分钟
TEST_INTERVAL = 30 * 60  # 30分钟
RETRY_DELAY = 5  # 重试延迟（秒）

# 代理配置
PROXY_FILE = "proxy.txt"

async def load_tokens_with_emails():
    """从token.txt文件中加载多个token和邮箱映射"""
    try:
        with open('token.txt', 'r') as file:
            token_email_mapping = {}
            for line in file:
                parts = line.strip().split(',')
                if len(parts) == 2:
                    token, email = parts
                    token_email_mapping[token] = email
            return token_email_mapping
    except FileNotFoundError:
        logging.error("token.txt文件未找到")
    except Exception as e:
        logging.error(f"从token.txt文件加载tokens和邮箱时发生错误: {e}")
    return {}

async def load_proxies():
    """从proxy.txt文件中加载多个代理"""
    try:
        with open(PROXY_FILE, 'r') as file:
            proxies = [line.strip() for line in file if line.strip()]
            return proxies
    except FileNotFoundError:
        logging.warning("proxy.txt文件未找到,将不使用代理")
    except Exception as e:
        logging.error(f"从proxy.txt文件加载代理时发生错误: {e}")
    return []

async def get_ip(proxy=None):
    """获取当前IP地址,可以使用代理"""
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        if proxy:
            session._connector._proxy = proxy
        try:
            async with session.get("https://api64.ipify.org?format=json", timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('ip')
        except Exception as e:
            logging.error(f"获取IP失败: {e}")
            await asyncio.sleep(RETRY_DELAY)
    return None

async def send_heartbeat(token, proxy=None):
    """发送心跳信号,可以使用代理"""
    ip = await get_ip(proxy)
    if not ip:
        return

    # 获取代理的IP地址
    proxy_ip = await get_ip(proxy) if proxy else "本地直连"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {"ip": ip}
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        if proxy:
            session._connector._proxy = proxy
        try:
            async with session.post(f"{BASE_URL}/heartbeat", headers=headers, json=data, timeout=5) as response:
                if response.status == 200:
                    logging.info(f"{Colors.GREEN}心跳发送成功{Colors.RESET}")
                    return
                elif response.status == 429:  # Rate limit error
                    return  # 静默处理限流错误
                error_message = await response.text()
                logging.error(f"发送心跳失败。状态: {response.status}, 错误信息: {error_message}")
        except Exception as e:
            logging.error(f"发送心跳时发生错误: {e}")

async def fetch_points(token, proxy=None):
    """获取当前分数,可以使用代理"""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        if proxy:
            session._connector._proxy = proxy
        try:
            async with session.get(f"{BASE_URL}/points", headers=headers, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('points')
        except Exception as e:
            logging.error(f"获取分数时发生错误: {e}")
            await asyncio.sleep(RETRY_DELAY)
    return None

async def test_all_nodes(nodes, proxy=None):
    """同时测试所有节点,可以使用代理"""
    async def test_single_node(node):
        try:
            start = asyncio.get_event_loop().time()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                if proxy:
                    session._connector._proxy = proxy
                async with session.get(f"http://{node['ip']}", timeout=5) as node_response:
                    latency = (asyncio.get_event_loop().time() - start) * 1000
                    status = "在线" if node_response.status == 200 else "离线"
                    latency_value = latency if status == "在线" else -1
                    logging.info(f"节点 {node['node_id']} 测试 ({node['ip']}) latency: {latency_value:.2f}ms, status: {status}")
                    return (node['node_id'], node['ip'], latency_value, status)
        except (asyncio.TimeoutError, aiohttp.ClientConnectorError):
            logging.info(f"节点 {node['node_id']} 测试 ({node['ip']}) latency: -1ms, status: 离线")
            return (node['node_id'], node['ip'], -1, "离线")

    tasks = [test_single_node(node) for node in nodes]
    return await asyncio.gather(*tasks)

async def report_node_result(token, node_id, ip, latency, status, proxy=None):
    """报告单个节点的测试结果,可以使用代理"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    test_data = {
        "node_id": node_id,
        "ip": ip,
        "latency": latency,
        "status": status
    }
    
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        if proxy:
            session._connector._proxy = proxy
        try:
            async with session.post(f"{BASE_URL}/test", headers=headers, json=test_data, timeout=5) as response:
                if response.status == 200:
                    logging.info(f"节点 {node_id} 结果报告成功")
                    return
                error_message = await response.text()
                logging.error(f"报告节点 {node_id} 结果失败。状态: {response.status}, 错误信息: {error_message}")
        except Exception as e:
            logging.error(f"报告节点 {node_id} 结果时发生错误: {e}")

async def report_all_node_results(token, results, proxy=None):
    """报告所有节点的测试结果,可以使用代理"""
    for node_id, ip, latency, status in results:
        await report_node_result(token, node_id, ip, latency, status, proxy)

async def start_testing(token, proxy=None):
    """开始测试流程,可以使用代理"""
    logging.info("正在测试节点...")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
        if proxy:
            session._connector._proxy = proxy
        try:
            async with session.get(f"{BASE_URL}/nodes", headers={"Authorization": f"Bearer {token}"}, timeout=5) as response:
                if response.status == 200:
                    nodes = await response.json()
                    results = await test_all_nodes(nodes, proxy)
                    await report_all_node_results(token, results, proxy)
                    return
                error_message = await response.text()
                logging.error(f"获取节点信息时发生错误。状态: {response.status}, 错误信息: {error_message}")
        except Exception as e:
            logging.error(f"获取节点信息时发生错误: {e}")

async def register_account():
    """注册新账户"""
    print("\n=== 账户注册 ===")
    email = input("请输入邮箱: ")
    password = input("请输入密码: ")
    
    # 进行注册
    async with aiohttp.ClientSession() as session:
        try:
            data = {
                "email": email,
                "password": password,
            }
            proxy = await load_proxies()
            if proxy:
                session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
                session._connector._proxy = proxy[0]  # 使用第一个代理进行注册
            async with session.post(f"{BASE_URL}/signup", json=data, timeout=5) as response:
                if response.status in [200, 201]:
                    result = await response.json()
                    token = result.get('token')
                    print(f"\n注册成功！")
                    print(f"您的token是: {token}")
                    print("请保存此信息到token.txt文件中")
                    
                    # 可选：自动保存注册信息到文件
                    save = input("\n是否自动保存注册信息到token.txt？(y/n): ")
                    if save.lower() == 'y':
                        try:
                            with open('token.txt', 'a') as f:
                                f.write(f"{token},{email}\n")
                            print("token和邮箱已成功添加到token.txt")
                            print("如需运行节点，请确保token.txt包含所有需要运行的token和对应的邮箱")
                        except Exception as e:
                            print(f"保存token和邮箱时发生错误: {e}")
                            print("请手动保存token和邮箱")
                    return
                error_message = await response.text()
                logging.error(f"注册失败。状态: {response.status}, 错误信息: {error_message}")
        except Exception as e:
            logging.error(f"注册过程中发生错误: {e}")

async def display_menu():
    """显示主菜单"""
    # await run_node()
    await register_account()

async def run_node():
    """运行节点测试并显示多个token的分数"""
    token_email_mapping = await load_tokens_with_emails()
    if not token_email_mapping:
        logging.error("无法加载tokens和邮箱。请确保token.txt文件存在且包含有效的tokens和邮箱。")
        return

    proxies = await load_proxies()
    
    logging.info("Tokens和邮箱加载成功!")
    
    # 获取并显示每个token的分数
    for i, (token, email) in enumerate(token_email_mapping.items()):
        proxy = proxies[i] if i < len(proxies) else None
        if proxy:
            print(f"{Colors.CYAN}使用代理进行分数获取: {proxy}{Colors.RESET}")
        else:
            print(f"{Colors.CYAN}使用本地直连进行分数获取{Colors.RESET}")
        current_points = await fetch_points(token, proxy)
        if current_points is not None:
            print(f"{Colors.GREEN}邮箱: {email} 当前分数: {current_points}{Colors.RESET}")
    
    next_heartbeat_time = datetime.now()
    next_test_time = datetime.now()
    first_heartbeat = True
    
    try:
        while True:
            current_time = datetime.now()
            
            if current_time >= next_heartbeat_time:
                if first_heartbeat:
                    logging.info("开始首次心跳...")
                    first_heartbeat = False
                for i, (token, email) in enumerate(token_email_mapping.items()):
                    proxy = proxies[i] if i < len(proxies) else None
                    if proxy:
                        print(f"{Colors.CYAN}使用代理进行心跳发送: {proxy}{Colors.RESET}")
                    else:
                        print(f"{Colors.CYAN}使用本地直连进行心跳发送{Colors.RESET}")
                    await send_heartbeat(token, proxy)
                next_heartbeat_time = current_time + timedelta(seconds=HEARTBEAT_INTERVAL)
            
            if current_time >= next_test_time:
                for i, (token, email) in enumerate(token_email_mapping.items()):
                    proxy = proxies[i] if i < len(proxies) else None
                    if proxy:
                        print(f"{Colors.CYAN}使用代理进行节点测试: {proxy}{Colors.RESET}")
                    else:
                        print(f"{Colors.CYAN}使用本地直连进行节点测试{Colors.RESET}")
                    await start_testing(token, proxy)
                    current_points = await fetch_points(token, proxy)
                    if current_points is not None:
                        print(f"{Colors.GREEN}邮箱: {email} 测试节点循环完成后当前分数: {current_points}{Colors.RESET}")
                next_test_time = current_time + timedelta(seconds=TEST_INTERVAL)
            
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n返回主菜单...")

async def main():
    await display_menu()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已退出")
        sys.exit(0)
