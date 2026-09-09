import time
from collections import Counter

print('time.time()   解析度:', time.get_clock_info('time').resolution)
print('monotonic     解析度:', time.get_clock_info('monotonic').resolution)
print()

cm, cw = Counter(), Counter()
pm = pw = None

for _ in range(120):                       # 兩分鐘
    now   = time.monotonic()
    now_w = time.time()
    if pm is not None:
        cm[round((now   - pm) * 1000)] += 1
        cw[round((now_w - pw) * 1000)] += 1
    pm, pw = now, now_w
    time.sleep(max(0, now + 1.0 - time.monotonic()))

print('monotonic 間隔：')
for ms in sorted(cm):
    print(f'  {ms}  {cm[ms]}')
print('wall clock 間隔：')
for ms in sorted(cw):
    print(f'  {ms}  {cw[ms]}')