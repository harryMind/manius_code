"""
测试从用户输入命令起到解析命令并转发控制下游命令
"""
import argparse 

# 1. 创建解析器  prog表示命令名称，一般在usage中出现
parser = argparse.ArgumentParser(prog="yhl",description="学习argparse命令行解析") 

# 2. 添加参数 python test.py --name 参数
parser.add_argument("--name", help="输入名称") 
parser.add_argument("--port",help="配置运行端口")
# action写上store_true表示该命令不需要参数，不写则需要参数
parser.add_argument("--version",help="查看版本",action="store_true")

# 自定义参数保存位置

sub_commands = parser.add_subparsers(dest="commands",title="子命令")
# 注册ping命令
ping_command = sub_commands.add_parser("ping")

# 注册run命令
run_command = sub_commands.add_parser("run")

# 3. 解析终端参数 
args = parser.parse_args() 

# 4. 使用参数 
if args.commands == "ping":
    print('已连通')
elif args.commands == "run":
    print('后台已启动')
