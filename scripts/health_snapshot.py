# PC 프로그램 실행 중 메모리/스레드/CPU 추이를 기록하는 모니터링 도구
# (Test Environment Deployment & E2E Validation Sprint 10/11/12절 —
# 8시간 장시간 테스트, Sleep/Wake 테스트, 네트워크 전환 테스트에서 공통으로
# "시작 시점 대비 변화"를 객관적으로 기록하기 위해 사용한다).
#
# Supabase/네트워크에 전혀 접근하지 않는다 — 로컬 프로세스 정보(psutil)와
# 로그 파일만 읽는다. 지정한 간격으로 CSV에 한 줄씩 추가하며, 매 줄마다
# flush하므로 도중에 강제 종료돼도 그때까지의 기록은 남는다.
#
# 사용법:
#   python scripts/health_snapshot.py --pid <cacao_macro.exe의 PID> \
#       --log-file test-runtime\logs\<최신 로그 파일> \
#       --output docs/test_results/long_run_8h_raw.csv \
#       --interval-seconds 300
#
# PID는 작업 관리자(세부 정보 탭)에서 확인하거나, PowerShell에서
#   Get-Process -Name cacao_macro | Select-Object Id
# 로 확인한다. Ctrl+C로 언제든 중단할 수 있다 — 그때까지 기록된 CSV는
# 그대로 유효하다.

import argparse
import csv
import os
import sys
import time
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 로그 파일에서 마지막으로 나타난 상태를 요약하기 위한 키워드 — 이 스크립트는
# 로그 "내용"을 요약해 CSV에 남기지 않는다(메시지 본문이 로그에 섞여 있을 수
# 있으므로) — 아래 키워드가 포함된 마지막 줄이 있었는지 여부(불리언)만 남긴다.
_RECONNECT_KEYWORDS = ("재연결", "RECONNECTING", "reconnect")
_SUBSCRIBED_KEYWORDS = ("실시간 연결됨", "SUBSCRIBED")
_ERROR_KEYWORDS = ("[오류]", "[경고]", "ERROR", "FAILED")


def _tail_log_flags(log_file: str, since_byte: int) -> tuple:
    """log_file의 since_byte 이후 새로 추가된 내용만 훑어 키워드 존재 여부와
    새 파일 크기를 반환한다. 파일이 없으면 전부 False, since_byte 그대로."""
    if not log_file or not os.path.exists(log_file):
        return False, False, False, since_byte

    saw_reconnect = saw_subscribed = saw_error = False
    try:
        size = os.path.getsize(log_file)
        if size < since_byte:
            since_byte = 0  # 로그 파일이 회전(rotate)된 것으로 간주하고 처음부터 다시 본다
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(since_byte)
            for line in f:
                if any(k in line for k in _RECONNECT_KEYWORDS):
                    saw_reconnect = True
                if any(k in line for k in _SUBSCRIBED_KEYWORDS):
                    saw_subscribed = True
                if any(k in line for k in _ERROR_KEYWORDS):
                    saw_error = True
            since_byte = f.tell()
    except OSError:
        pass
    return saw_reconnect, saw_subscribed, saw_error, since_byte


def run(pid: int, log_file: str, output: str, interval_seconds: int, duration_seconds: int) -> int:
    import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        print(f"[오류] PID {pid} 프로세스를 찾을 수 없습니다.")
        return 1

    header = [
        "timestamp", "elapsed_seconds", "process_running", "rss_mb", "num_threads",
        "cpu_percent", "saw_reconnect_since_last", "saw_subscribed_since_last",
        "saw_error_since_last",
    ]
    write_header = not os.path.exists(output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    start_time = time.monotonic()
    since_byte = 0
    if log_file and os.path.exists(log_file):
        since_byte = os.path.getsize(log_file)  # 시작 시점 이후 새로 쌓이는 내용만 본다

    print(f"[시작] PID={pid} 모니터링 시작 — {interval_seconds}초 간격, 출력: {output}")
    print("Ctrl+C로 언제든 중단할 수 있습니다.")

    with open(output, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
            f.flush()

        try:
            while True:
                elapsed = time.monotonic() - start_time
                running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
                if running:
                    rss_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
                    num_threads = proc.num_threads()
                    cpu_percent = proc.cpu_percent(interval=1.0)
                else:
                    rss_mb = num_threads = cpu_percent = ""

                reconnect, subscribed, error, since_byte = _tail_log_flags(log_file, since_byte)

                writer.writerow([
                    datetime.now().isoformat(timespec="seconds"), round(elapsed), running,
                    rss_mb, num_threads, cpu_percent, reconnect, subscribed, error,
                ])
                f.flush()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] running={running} rss_mb={rss_mb} threads={num_threads} cpu%={cpu_percent}")

                if duration_seconds and elapsed >= duration_seconds:
                    print("[완료] 지정한 duration에 도달했습니다.")
                    break
                if not running:
                    print("[안내] 프로세스가 더 이상 실행 중이 아닙니다 — 기록을 종료합니다.")
                    break

                time.sleep(max(0, interval_seconds - 1))
        except KeyboardInterrupt:
            print("\n[중단] 사용자가 Ctrl+C로 중단했습니다 — 그동안의 기록은 저장되어 있습니다.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PC 프로그램 실행 중 메모리/스레드/CPU 추이 기록(로컬 전용, 네트워크 접근 없음)")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--log-file", default="")
    parser.add_argument("--output", default="docs/test_results/long_run_8h_raw.csv")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--duration-seconds", type=int, default=0, help="0이면 Ctrl+C할 때까지 무제한")
    args = parser.parse_args()
    return run(args.pid, args.log_file, args.output, args.interval_seconds, args.duration_seconds)


if __name__ == "__main__":
    sys.exit(main())
