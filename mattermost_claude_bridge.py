#!/usr/bin/env python3
"""
Mattermost <-> Claude Code 브리지

- Mattermost WebSocket으로 메시지를 수신
- 봇에게 보낸 DM 또는 봇을 멘션한 메시지를 Claude Code(claude -p, 헤드리스)로 처리
- 결과를 같은 채널에 다시 게시
- 채널별 세션(session_id)을 JSON 파일에 저장/복원하여 재시작 후에도 대화 맥락 유지

실행 (맥 미니 호스트에서):
    export MM_URL="https://chat.example.com"
    export MM_TOKEN="봇_액세스_토큰"
    export CLAUDE_WORKDIR="/path/to/대상폴더"
    # (선택) 세션 저장 파일 경로. 기본값은 스크립트 옆 bridge_sessions.json
    export SESSIONS_FILE="/path/to/bridge_sessions.json"
    # (선택) 허용 사용자 ID를 콤마로. 비우면 모두 허용
    export ALLOWED_USER_IDS=""
    python3 mattermost_claude_bridge.py

의존성:
    pip3 install websocket-client requests
"""

import json
import os
import subprocess
import threading
import time

import requests
import websocket  # pip install websocket-client

MM_URL = os.environ["MM_URL"].rstrip("/")
MM_TOKEN = os.environ["MM_TOKEN"]
WORKDIR = os.environ.get("CLAUDE_WORKDIR", os.path.expanduser("~"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
MCP_CONFIG = os.environ.get("MCP_CONFIG", "")
ALLOWED = {u for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u}
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
# 멘션 없이도 항상 응답할 채널 ID 목록 (쉼표 구분). 세션 등록과 무관하게 상시 반응.
ALWAYS_CHANNELS = {c.strip() for c in os.environ.get("MM_ALWAYS_CHANNELS", "").split(",") if c.strip()}

# 세션 매핑 저장 파일 (기본: 스크립트와 같은 폴더)
SESSIONS_FILE = os.environ.get(
    "SESSIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_sessions.json"),
)

WS_URL = MM_URL.replace("https://", "wss://").replace("http://", "ws://") + "/api/v4/websocket"
HEADERS = {"Authorization": f"Bearer {MM_TOKEN}"}

# 채널별 Claude Code 세션 ID. 파일에서 복원하고, 갱신 시 파일에 저장.
_sessions_lock = threading.Lock()


def load_sessions():
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_sessions():
    # 원자적 저장: 임시 파일에 기록 후 교체하여, 쓰기 중 중단되어도 원본이 손상되지 않게 함
    tmp = SESSIONS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSIONS_FILE)
    except OSError as e:
        print(f"[bridge] 세션 저장 실패: {e}")


sessions = load_sessions()


def api_get(path):
    r = requests.get(f"{MM_URL}/api/v4{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def api_get_with_retry(path, max_attempts=None, base_delay=5, max_delay=60):
    """서버 일시 장애(502/530 등)나 네트워크 오류에 죽지 않도록 지수 백오프로 재시도.

    max_attempts=None 이면 무한 재시도(서버가 돌아올 때까지 대기). 시작 시점에는
    KeepAlive 크래시 루프에 빠지느니 프로세스를 살려두고 붙을 때까지 기다리는 편이 낫다.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return api_get(path)
        except (requests.RequestException, OSError) as e:
            if max_attempts is not None and attempt >= max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            print(
                f"[bridge] {path} 호출 실패({e}). {delay}s 후 재시도 (attempt {attempt})",
                flush=True,
            )
            time.sleep(delay)


def post_message(channel_id, text, max_attempts=4, base_delay=3, max_delay=30):
    """응답 게시. 서버 일시 오류(502/530 등)로 응답이 유실되지 않도록 몇 번 재시도."""
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(
                f"{MM_URL}/api/v4/posts",
                headers=HEADERS,
                json={"channel_id": channel_id, "message": text},
                timeout=30,
            )
            r.raise_for_status()
            return
        except (requests.RequestException, OSError) as e:
            if attempt >= max_attempts:
                print(f"[bridge] 응답 게시 최종 실패 | channel={channel_id} | {e}", flush=True)
                return
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            print(
                f"[bridge] 응답 게시 실패({e}). {delay}s 후 재시도 (attempt {attempt})",
                flush=True,
            )
            time.sleep(delay)


me = api_get_with_retry("/users/me")
BOT_ID = me["id"]
BOT_USERNAME = me["username"]
print(f"[bridge] 봇 연결: @{BOT_USERNAME} ({BOT_ID})")
print(f"[bridge] 세션 파일: {SESSIONS_FILE} (복원된 채널 {len(sessions)}개)")


def _invoke(prompt, resume_id):
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        # 무인 실행을 위해 권한 확인을 건너뜀.
        # 읽기 전용 어시스턴트로만 쓰려면 아래 줄을 지우고
        # 대신 '--allowedTools', 'Read,Grep,Glob' 를 추가하세요.
        "--dangerously-skip-permissions",
    ]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if MCP_CONFIG:
        cmd += ["--mcp-config", MCP_CONFIG]
    if resume_id:
        cmd += ["--resume", resume_id]
    # '--' 로 옵션 파싱을 종료하고 프롬프트를 마지막 위치에 둔다.
    # 사용자가 '--post-only' 처럼 '-'로 시작하는 문장을 보내도
    # CLI가 옵션으로 오인하지 않도록 방어.
    cmd += ["--", prompt]
    return subprocess.run(
        cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT
    )


def run_claude(channel_id, prompt):
    """Claude Code를 헤드리스로 실행하고 결과를 채널에 게시."""
    resume_id = sessions.get(channel_id)
    out = None
    print(f"[bridge] claude 실행 시작 | channel={channel_id} | resume={resume_id} | prompt={prompt[:50]!r}", flush=True)
    try:
        out = _invoke(prompt, resume_id)
        print(f"[bridge] claude 종료 | returncode={out.returncode} | stdout={out.stdout[:200]!r} | stderr={out.stderr[:200]!r}", flush=True)

        # 저장된 세션이 만료/삭제되어 --resume가 실패한 경우, 새 세션으로 1회 재시도.
        # 단, 재시도가 실제 출력을 냈을 때만 기존 세션을 폐기한다.
        # (입력 파싱 오류 등으로 두 번 다 빈 출력이면 세션을 보존해 채널이 먹통되지 않게 함)
        if resume_id and out.returncode != 0:
            print(f"[bridge] resume 실패 추정, 새 세션으로 재시도: {channel_id}", flush=True)
            retry = _invoke(prompt, None)
            print(f"[bridge] 재시도 결과 | returncode={retry.returncode} | stdout={retry.stdout[:200]!r}", flush=True)
            if retry.stdout.strip():
                with _sessions_lock:
                    sessions.pop(channel_id, None)
                    save_sessions()
                out = retry
            # else: 재시도도 빈 출력 → 원래 에러 유지, 기존 세션 보존

        data = json.loads(out.stdout)

        # 새 session_id 저장 (재시작에도 유지)
        if data.get("session_id"):
            with _sessions_lock:
                sessions[channel_id] = data["session_id"]
                save_sessions()

        reply = data.get("result") or "(빈 응답)"
        if data.get("is_error"):
            reply = f"Claude Code 오류:\n{reply}"
    except subprocess.TimeoutExpired:
        reply = f"처리 시간이 초과되었습니다({CLAUDE_TIMEOUT}s)."
        print(f"[bridge] 타임아웃: {channel_id}", flush=True)
    except json.JSONDecodeError:
        stderr = (out.stderr if out else "")[:800]
        reply = f"출력 파싱 실패. stderr:\n{stderr}"
        print(f"[bridge] JSON 파싱 실패 | stderr={stderr!r}", flush=True)
    except Exception as e:
        reply = f"오류: {e}"
        print(f"[bridge] 예외 발생: {type(e).__name__}: {e}", flush=True)

    print(f"[bridge] 응답 전송 | channel={channel_id} | reply={reply[:80]!r}", flush=True)
    post_message(channel_id, reply)


def on_message(ws, message):
    event = json.loads(message)
    if event.get("event") != "posted":
        return

    data = event["data"]
    post = json.loads(data["post"])

    # 봇 자신의 메시지는 무시 (무한 루프 방지)
    if post["user_id"] == BOT_ID:
        return

    # 허용 사용자 제한 (설정한 경우)
    # if ALLOWED and post["user_id"] not in ALLOWED:
    #     return

    channel_id = post["channel_id"]
    channel_type = data.get("channel_type")  # 'D'=DM, 'O'=공개, 'P'=비공개
    text = post["message"]
    mention = f"@{BOT_USERNAME}"
    mentioned = mention in text

    # DM, 멘션, 세션 등록 채널, 또는 상시 응답 채널(ALWAYS_CHANNELS)이면 반응
    if (channel_type != "D" and not mentioned
            and channel_id not in sessions and channel_id not in ALWAYS_CHANNELS):
        print(f"[bridge] 무시 | channel={channel_id} type={channel_type} mentioned={mentioned}", flush=True)
        return

    prompt = text.replace(mention, "").strip()
    if not prompt:
        return
    print(f"[bridge] 메시지 수신 | channel={channel_id} | text={text[:80]!r}", flush=True)
    post_message(channel_id, "_처리 중..._")
    threading.Thread(
        target=run_claude, args=(channel_id, prompt), daemon=True
    ).start()


def on_open(ws):
    ws.send(json.dumps({
        "seq": 1,
        "action": "authentication_challenge",
        "data": {"token": MM_TOKEN},
    }))
    print("[bridge] WebSocket 연결 및 인증 요청 전송")


def on_error(ws, error):
    print(f"[bridge] WS 오류: {error}")


def on_close(ws, code, msg):
    print(f"[bridge] WS 종료: {code} {msg}")


if __name__ == "__main__":
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    # 연결이 끊겨도 5초 후 자동 재연결, 30초마다 ping
    ws.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)