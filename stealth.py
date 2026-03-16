#!/usr/bin/env python3
"""
Chess Advisor — Stealth Mode
Type "srg2" anywhere while Chess.app is open → see the perfect move.
Runs silently in the background. No terminal input needed.
Tracks game state automatically via Chess.app screen capture.
"""

import chess
import chess.engine
import sys
import os
import re
import time
import subprocess
import tempfile
import threading
import multiprocessing
from pynput import keyboard

# ── Screen capture ────────────────────────────────────────────────────
try:
    import Quartz
    from PIL import Image
    VISION = True
except ImportError:
    VISION = False

# ── Config ────────────────────────────────────────────────────────────
STOCKFISH = "/opt/homebrew/bin/stockfish"
THREADS = max(1, multiprocessing.cpu_count() - 1)
HASH_MB = 2048
ANALYSIS_TIME = 3.0
DEEP_TIME = 6.0
MULTIPV = 3
POLL_INTERVAL = 0.5
DIFF_THRESHOLD = 20
CHEAT_CODE = "srg2"
PIECE_NAMES = {
    chess.PAWN: "Pawn", chess.KNIGHT: "Knight", chess.BISHOP: "Bishop",
    chess.ROOK: "Rook", chess.QUEEN: "Queen", chess.KING: "King",
}


def notify(title, message):
    """Show macOS notification."""
    # Escape for AppleScript
    t = title.replace('"', '\\"').replace("'", "'")
    m = message.replace('"', '\\"').replace("'", "'")
    subprocess.run([
        'osascript', '-e',
        f'display notification "{m}" with title "{t}" sound name "Pop"'
    ], capture_output=True, timeout=5)


def say(text):
    """Speak text aloud (optional, for move announcements)."""
    subprocess.Popen(['say', '-r', '200', text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ═══════════════════════════════════════════════════════════════════════
#  BOARD READER — Detects board state from Chess.app
# ═══════════════════════════════════════════════════════════════════════

class BoardReader:
    """Reads Chess.app state via screen capture and window title."""

    def __init__(self):
        self.window_id = None
        self.col_edges = None
        self.row_top = None
        self.row_bottom = None
        self.calibrated = False
        self.board = chess.Board()
        self.prev_image = None
        self.playing_as = None  # Detected automatically
        self.tracking = False   # Whether we're tracking a game

    def find_window(self):
        """Find Chess.app window."""
        if not VISION:
            return False
        try:
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
            for w in windows:
                if w.get('kCGWindowOwnerName') != 'Chess':
                    continue
                name = w.get('kCGWindowName', '')
                bounds = w.get('kCGWindowBounds', {})
                height = bounds.get('Height', 0)
                if 'Game' in name and height > 200:
                    self.window_id = w['kCGWindowNumber']
                    return True
        except Exception:
            pass
        return False

    def get_title(self):
        """Get Chess.app window title."""
        if self.window_id is None:
            return None
        try:
            info = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionIncludingWindow, self.window_id
            )
            if info and len(info) > 0:
                return info[0].get('kCGWindowName', '')
        except Exception:
            pass
        return None

    def capture(self):
        """Capture Chess.app window."""
        if self.window_id is None:
            return None
        try:
            fd, tmp = tempfile.mkstemp(suffix='.png', prefix='chess_')
            os.close(fd)
            result = subprocess.run(
                ['screencapture', '-l', str(self.window_id), '-x', '-o', tmp],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                os.unlink(tmp)
                return None
            img = Image.open(tmp)
            img.load()
            os.unlink(tmp)
            return img
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return None

    def calibrate(self, img):
        """Detect board grid from image."""
        w, h = img.size
        pixels = img.load()
        cy = h // 2

        # Find board horizontal extent
        left = right = 0
        for x in range(w):
            r, g, b = pixels[x, cy][:3]
            if self._is_board(r, g, b):
                left = x
                break
        for x in range(w - 1, -1, -1):
            r, g, b = pixels[x, cy][:3]
            if self._is_board(r, g, b):
                right = x
                break

        if right - left < 100:
            return False

        board_width = right - left
        sq_w = board_width / 8
        self.col_edges = [int(left + i * sq_w) for i in range(9)]

        # Find vertical extent
        cx = w // 2
        top = bottom = 0
        for y in range(50, h):
            r, g, b = pixels[cx, y][:3]
            if self._is_board(r, g, b):
                top = y
                break
        for y in range(h - 1, 0, -1):
            r, g, b = pixels[cx, y][:3]
            if self._is_board(r, g, b):
                bottom = y
                break

        if bottom - top < 100:
            return False

        self.row_top = top
        self.row_bottom = bottom
        self.calibrated = True
        return True

    def _is_board(self, r, g, b):
        if r < 40 and g < 40 and b < 40:
            return False
        if abs(r - g) < 10 and abs(g - b) < 10 and r < 80:
            return False
        if r > 80 and (r - b) > 5:
            return True
        if (r + g + b) > 300:
            return True
        return False

    def _square_center(self, file, rank):
        x = (self.col_edges[file] + self.col_edges[file + 1]) // 2
        row_h = (self.row_bottom - self.row_top) / 8
        y = int(self.row_top + (7 - rank + 0.5) * row_h)
        return x, y

    def _square_diff(self, px1, px2, w, h, file, rank):
        cx, cy = self._square_center(file, rank)
        sq_w = (self.col_edges[1] - self.col_edges[0]) if len(self.col_edges) > 1 else 100
        half = max(5, int(sq_w * 0.25))
        total = count = 0
        for dy in range(-half, half + 1, 3):
            for dx in range(-half, half + 1, 3):
                px, py = cx + dx, cy + dy
                if 0 <= px < w and 0 <= py < h:
                    r1, g1, b1 = px1[px, py][:3]
                    r2, g2, b2 = px2[px, py][:3]
                    total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
                    count += 1
        return total / max(count, 1) / 3

    def detect_changed_squares(self, before, after):
        px1, px2 = before.load(), after.load()
        w, h = min(before.width, after.width), min(before.height, after.height)
        changed = []
        for rank in range(8):
            for file in range(8):
                diff = self._square_diff(px1, px2, w, h, file, rank)
                if diff > DIFF_THRESHOLD:
                    changed.append((file, rank, diff))
        changed.sort(key=lambda x: -x[2])
        return changed

    def deduce_move(self, changed, board):
        if len(changed) < 2:
            return None
        changed_set = {chess.square(f, r) for f, r, _ in changed}
        best_match, best_score = None, -1

        for move in board.legal_moves:
            affected = {move.from_square, move.to_square}
            if board.is_castling(move):
                rank = chess.square_rank(move.from_square)
                if board.is_kingside_castling(move):
                    affected.update({chess.square(7, rank), chess.square(5, rank)})
                else:
                    affected.update({chess.square(0, rank), chess.square(3, rank)})
            if board.is_en_passant(move):
                cap_sq = chess.square(chess.square_file(move.to_square),
                                     chess.square_rank(move.from_square))
                affected.add(cap_sq)
            overlap = len(affected & changed_set)
            noise = len(changed_set - affected)
            score = overlap * 10 - noise
            if overlap >= len(affected) and score > best_score:
                best_score = score
                best_match = move

        return best_match

    def detect_color(self):
        """Auto-detect which color the user is playing."""
        title = self.get_title()
        if title is None:
            return None
        # If the game just started and it says "White to Move", user is probably white
        # We'll refine this based on board orientation (pieces at bottom)
        if not self.board.move_stack:
            # New game — check board orientation from image
            img = self.capture()
            if img and self.calibrated:
                pixels = img.load()
                # Check bottom-left corner — if it has a white rook, user is white
                cx, cy = self._square_center(0, 0)  # a1
                # Sample center pixels
                r, g, b = pixels[cx, cy][:3]
                brightness = (r + g + b) / 3
                # Bottom row pieces: if bright piece on dark square = white pieces at bottom
                # a1 is a dark square, so white rook would appear bright
                if brightness > 120:
                    return chess.WHITE
                else:
                    return chess.BLACK
        return chess.WHITE  # Default


# ═══════════════════════════════════════════════════════════════════════
#  STEALTH ADVISOR — Background service
# ═══════════════════════════════════════════════════════════════════════

class StealthAdvisor:
    """Runs silently. Type 'srg2' to get the best move."""

    def __init__(self):
        self.reader = BoardReader()
        self.engine = None
        self.active = False
        self.code_buffer = ""
        self.lock = threading.Lock()
        self.game_board = chess.Board()
        self.tracking = False
        self.playing_as = chess.WHITE
        self.prev_image = None
        self.monitor_thread = None

    def start_engine(self):
        if self.engine is None:
            self.engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH)
            self.engine.configure({
                "Threads": THREADS,
                "Hash": HASH_MB,
                "Skill Level": 20,
                "UCI_LimitStrength": False,
                "UCI_ShowWDL": True,
                "Ponder": False,
            })

    def stop_engine(self):
        if self.engine:
            try:
                self.engine.quit()
            except Exception:
                pass
            self.engine = None

    def analyze(self):
        """Analyze current position and show best move via notification."""
        with self.lock:
            try:
                if not self.reader.find_window():
                    notify("Chess Advisor", "Chess.app not found. Open a game first.")
                    return

                # Capture and calibrate if needed
                img = self.reader.capture()
                if img is None:
                    notify("Chess Advisor", "Cannot capture Chess.app. Check Screen Recording permission.")
                    return

                if not self.reader.calibrated:
                    if not self.reader.calibrate(img):
                        notify("Chess Advisor", "Cannot detect board. Make sure Chess.app is visible.")
                        return

                # If not tracking yet, try to read game from Chess.app auto-save
                if not self.tracking:
                    self._start_tracking(img)

                # If we're tracking, detect any new moves
                if self.tracking and self.prev_image is not None:
                    self._detect_new_moves(img)

                self.prev_image = img

                # Now analyze
                self.start_engine()
                board = self.game_board

                if board.is_game_over():
                    notify("Chess Advisor", "Game is over.")
                    return

                # Determine if it's our turn
                title = self.reader.get_title() or ""
                if "White to Move" in title:
                    current_turn = chess.WHITE
                elif "Black to Move" in title:
                    current_turn = chess.BLACK
                else:
                    current_turn = board.turn

                # Deep analysis
                is_critical = board.is_check() or len(board.piece_map()) <= 10
                think = DEEP_TIME if is_critical else ANALYSIS_TIME

                results = self.engine.analyse(
                    board,
                    chess.engine.Limit(time=think),
                    multipv=MULTIPV,
                )

                # Format results
                best = results[0]
                best_move = best["pv"][0]
                best_san = board.san(best_move)

                # Score
                score = best["score"].white() if self.playing_as == chess.WHITE else best["score"].black()
                if score.is_mate():
                    m = score.mate()
                    score_str = f"Mate in {m}" if m > 0 else f"Getting mated in {abs(m)}"
                else:
                    cp = (score.score() or 0) / 100
                    if cp > 3:
                        score_str = f"+{cp:.1f} (winning)"
                    elif cp > 1:
                        score_str = f"+{cp:.1f} (better)"
                    elif cp > 0.3:
                        score_str = f"+{cp:.1f} (slight edge)"
                    elif cp >= -0.3:
                        score_str = f"{cp:+.1f} (equal)"
                    elif cp >= -1:
                        score_str = f"{cp:+.1f} (slightly worse)"
                    else:
                        score_str = f"{cp:+.1f} (worse)"

                # Describe best move
                piece = board.piece_at(best_move.from_square)
                piece_name = PIECE_NAMES.get(piece.piece_type, "?") if piece else "?"
                from_sq = chess.square_name(best_move.from_square)
                to_sq = chess.square_name(best_move.to_square)

                if board.is_castling(best_move):
                    if board.is_kingside_castling(best_move):
                        desc = "Castle kingside (O-O)"
                    else:
                        desc = "Castle queenside (O-O-O)"
                else:
                    captured = board.piece_at(best_move.to_square)
                    desc = f"{piece_name} {from_sq} → {to_sq}"
                    if captured:
                        desc += f" takes {PIECE_NAMES.get(captured.piece_type, '?')}"
                    if best_move.promotion:
                        desc += f" promote to {PIECE_NAMES.get(best_move.promotion, '?')}"

                # WDL
                wdl = best.get("wdl")
                wdl_str = ""
                if wdl:
                    w, d, l = wdl
                    if self.playing_as == chess.BLACK:
                        w, l = l, w
                    total = w + d + l
                    if total > 0:
                        wdl_str = f"\nWin: {w/total*100:.0f}%  Draw: {d/total*100:.0f}%  Loss: {l/total*100:.0f}%"

                # Alt moves
                alts = ""
                for i, info in enumerate(results[1:3], 2):
                    alt_move = info["pv"][0]
                    alt_san = board.san(alt_move)
                    alt_score = info["score"].white() if self.playing_as == chess.WHITE else info["score"].black()
                    if alt_score.is_mate():
                        alt_sc = f"M{abs(alt_score.mate())}"
                    else:
                        alt_sc = f"{(alt_score.score() or 0)/100:+.1f}"
                    alts += f"\n#{i}: {alt_san} [{alt_sc}]"

                move_num = board.fullmove_number
                msg = f"Move {move_num}: {best_san}\n{desc}\nEval: {score_str}{wdl_str}{alts}"

                notify("♟ PLAY: " + best_san, msg)

                # Also speak the move
                say(f"Play {best_san}")

            except Exception as e:
                notify("Chess Advisor Error", str(e)[:100])

    def _start_tracking(self, img):
        """Start tracking the game — try to read game state."""
        # Try to read PGN from Chess.app auto-save
        pgn_path = os.path.expanduser(
            "~/Library/Containers/com.apple.Chess/Data/Library/Application Support/Chess/Autosave.game"
        )
        if os.path.exists(pgn_path):
            try:
                with open(pgn_path, 'r') as f:
                    content = f.read()
                import chess.pgn
                import io
                game = chess.pgn.read_game(io.StringIO(content))
                if game:
                    self.game_board = chess.Board()
                    for move in game.mainline_moves():
                        self.game_board.push(move)
                    self.tracking = True
                    self.playing_as = self.reader.detect_color() or chess.WHITE
                    return
            except Exception:
                pass

        # Fallback: start from current position (assume new game or detect from title)
        title = self.reader.get_title() or ""
        move_match = re.search(r'Move\s+(\d+)', title)
        if move_match:
            move_num = int(move_match.group(1))
            if move_num <= 1:
                # New game, start fresh
                self.game_board = chess.Board()
                self.tracking = True
                self.playing_as = self.reader.detect_color() or chess.WHITE
                return

        # Can't determine position — start fresh and hope for the best
        self.game_board = chess.Board()
        self.tracking = True
        self.playing_as = self.reader.detect_color() or chess.WHITE

    def _detect_new_moves(self, img):
        """Detect if any new moves happened since last check."""
        if self.prev_image is None:
            return

        changed = self.reader.detect_changed_squares(self.prev_image, img)
        if not changed:
            return

        move = self.reader.deduce_move(changed, self.game_board)
        if move and move in self.game_board.legal_moves:
            self.game_board.push(move)

    def start_monitor(self):
        """Background thread that continuously tracks moves."""
        def monitor():
            while self.active:
                try:
                    if self.tracking and self.reader.calibrated:
                        img = self.reader.capture()
                        if img:
                            self._detect_new_moves(img)
                            self.prev_image = img
                except Exception:
                    pass
                time.sleep(POLL_INTERVAL)

        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()

    def on_key(self, key):
        """Global key listener — watches for cheat code."""
        try:
            char = key.char
            if char is None:
                return
        except AttributeError:
            return

        self.code_buffer += char
        # Keep only last N characters
        if len(self.code_buffer) > 20:
            self.code_buffer = self.code_buffer[-20:]

        if self.code_buffer.endswith(CHEAT_CODE):
            self.code_buffer = ""
            # Run analysis in a separate thread to not block key listener
            threading.Thread(target=self.analyze, daemon=True).start()

    def run(self):
        """Start the stealth advisor."""
        print("♟  Chess Advisor — Stealth Mode")
        print(f"   Stockfish 18 · {THREADS} threads · {HASH_MB}MB hash")
        print(f"   Type '{CHEAT_CODE}' anywhere while Chess.app is open")
        print(f"   Best move appears as a notification")
        print(f"   Press Ctrl+C to quit\n")

        if not VISION:
            print("   ⚠ Install Pillow + pyobjc: pip3 install Pillow pyobjc-framework-Quartz")
            sys.exit(1)

        self.active = True
        self.start_monitor()

        notify("Chess Advisor Active",
               f"Type '{CHEAT_CODE}' during a Chess.app game to see the perfect move.")

        # Start global key listener
        with keyboard.Listener(on_press=self.on_key) as listener:
            try:
                listener.join()
            except KeyboardInterrupt:
                pass

        self.active = False
        self.stop_engine()
        print("\n   Chess Advisor stopped.")


if __name__ == "__main__":
    StealthAdvisor().run()
