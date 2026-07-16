"""
这个文件将会学习本项目设计的asynico基本操作
"""
import asyncio
# async def 定义的函数为协程函数，调用时获得协程对象(可等待) 也就是可是用await

async def sleep():
    print('插队执行sleep()')
    print("loop: ",asyncio.get_running_loop())
    print("task: ",asyncio.all_tasks())
    await asyncio.sleep(2)

async def read_name():
    await sleep()
    return "are you ok ?"

# 同步阻塞操作必须创建独立线程后台执行

def read_file() -> str:
    return "文件内容"



async def run(name: str):
    # 事件循环暂停当前协程，立即插队执行await的协程对象
    task = asyncio.create_task(read_name())
    # 将协程对象创建task后，会加入循环队列

    # to_thread返回的是一个协程对象，也可以使用create_task加入循环队列不用立即执行
    content = await asyncio.to_thread(read_file)

    # 但凡await都会立即执行
    res = await task
    print(name+' '+res+" "+content)


if __name__ == '__main__':
    # asyncio.run会创建一个EventLoop并将传入的函数作为第一个顶层的task加入循环队列
    # 启动事件循环，循环不断取出就绪 Task 执行；
    asyncio.run(run('yhl'),debug=True)