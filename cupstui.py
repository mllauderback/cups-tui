#!/usr/bin/env python3
"""
printer_tui.py - A curses-based TUI that shows a live list of printers on Linux.

Uses CUPS command-line tools (lpstat) to gather printer info, so CUPS must be
installed and running (the 'cups-client' package on most distros).

Controls:
    q / Q       - quit
    r / R       - force refresh immediately
    Up/Down     - move selection (queue pane updates automatically)
    Enter       - on "+ Add Printer": open a form to add a new printer
                  via lpadmin; on a printer: toggle it as default
                  (asks to confirm)

Refreshes automatically every 2 seconds.
"""

import curses
import os
import shutil
import subprocess
import threading
import time


REFRESH_SECONDS = 2


def run(cmd):
    """Run a shell command and return stdout as a list of lines (empty list on error)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3
        )
        if out.returncode != 0:
            return []
        return out.stdout.splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def get_printer_data():
    """
    Single `lpstat -t` call that returns default printer, printer states,
    accepting status, and job counts all at once - much faster on startup
    than making several separate lpstat calls in sequence.

    Returns (printers_list, default_printer).
    """
    lines = run(["lpstat", "-t"])
    printers = {}
    default_printer = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("system default destination:"):
            default_printer = line.split(":", 1)[1].strip()

        elif line.startswith("printer "):
            parts = line.split()
            name = parts[1]
            state = "unknown"
            if " is " in line:
                after = line.split(" is ", 1)[1]
                state = after.split(".")[0].strip()
            printers.setdefault(
                name, {"name": name, "state": "unknown", "accepting": "?", "jobs": 0}
            )
            printers[name]["state"] = state

        elif "accepting requests" in line:
            parts = line.split()
            if parts:
                name = parts[0]
                accepting = "no" if "not accepting requests" in line else "yes"
                printers.setdefault(
                    name, {"name": name, "state": "unknown", "accepting": "?", "jobs": 0}
                )
                printers[name]["accepting"] = accepting

        else:
            # Job lines look like: "<printer>-<id>  user  size  date"
            parts = line.split()
            if parts and "-" in parts[0]:
                token = parts[0]
                pname = token.rsplit("-", 1)[0]
                if pname in printers:
                    printers[pname]["jobs"] += 1

    return sorted(printers.values(), key=lambda p: p["name"].lower()), default_printer


def draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
         loading=False, queue=None, queue_for=None, queue_loading=False):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    title = " Live Printer List (CUPS) "
    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(0, 0, title.center(w)[:w - 1])
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

    if not cups_available:
        msg = "lpstat not found - is CUPS (cups-client) installed?"
        stdscr.addstr(2, 2, msg[:w - 4], curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(h - 1, 0, " q: quit ".center(w)[:w - 1], curses.color_pair(1))
        stdscr.refresh()
        return

    queue = queue or []

    # Split the screen into a left pane (printer list) and a right pane
    # (job queue for the currently selected printer).
    left_w = max(24, min(38, w // 3))
    if left_w >= w - 20:
        left_w = max(15, w // 2 - 1)
    divider_col = left_w
    right_x = divider_col + 2
    right_w = max(10, w - right_x - 1)

    # --- Left pane: printer list ---
    stdscr.addstr(2, 2, "PRINTERS", curses.A_BOLD | curses.A_UNDERLINE)

    add_row = 3
    add_label = "+ Add Printer"[: left_w - 3]
    add_attr = curses.color_pair(4) | curses.A_BOLD
    if selected == 0:
        add_attr |= curses.A_REVERSE
    stdscr.addstr(add_row, 2, add_label, add_attr)

    if not printers:
        empty_msg = "Searching for printers..." if loading else "No printers found."
        stdscr.addstr(add_row + 2, 2, empty_msg[: left_w - 3], curses.A_DIM)
    else:
        for i, p in enumerate(printers):
            row = add_row + 1 + i
            if row >= h - 2:
                break
            name = p["name"]
            if name == default_printer:
                name += " *"
            state = p["state"]
            accepting = p["accepting"]
            jobs = p["jobs"]

            label = f"{name} [{state}, {accepting}]" + (f" ({jobs})" if jobs else "")
            label = label[: left_w - 3]

            attr = curses.color_pair(0)
            if "idle" in state.lower():
                attr = curses.color_pair(2)
            elif "disabled" in state.lower() or accepting == "no":
                attr = curses.color_pair(3)

            if selected == i + 1:
                attr |= curses.A_REVERSE

            stdscr.addstr(row, 2, label, attr)

    # --- Divider ---
    for row in range(2, h - 2):
        try:
            stdscr.addch(row, divider_col, curses.ACS_VLINE)
        except curses.error:
            pass

    # --- Right pane: job queue for the selected printer ---
    queue_title = f"QUEUE: {queue_for}" if queue_for else "QUEUE"
    stdscr.addstr(2, right_x, queue_title[:right_w], curses.A_BOLD | curses.A_UNDERLINE)

    col_header = f"{'JOB ID':<18}{'USER':<12}{'SIZE':<10}{'SUBMITTED':<24}"
    stdscr.addstr(3, right_x, col_header[:right_w], curses.A_UNDERLINE)

    if selected == 0:
        stdscr.addstr(5, right_x, "Press Enter to add a new printer."[:right_w], curses.A_DIM)
    elif not printers:
        pass
    elif queue_loading and not queue:
        stdscr.addstr(5, right_x, "Loading queue..."[:right_w], curses.A_DIM)
    elif not queue:
        stdscr.addstr(5, right_x, "No jobs in queue."[:right_w], curses.A_DIM)
    else:
        for i, j in enumerate(queue):
            row = 4 + i
            if row >= h - 2:
                break
            line = f"{j['id']:<18}{j['user']:<12}{j['size']:<10}{j['date']:<24}"
            stdscr.addstr(row, right_x, line[:right_w])

    footer = " q: quit | r: refresh | up/down: select | enter: toggle default / add | * = default "
    stdscr.attron(curses.color_pair(1))
    stdscr.addstr(h - 1, 0, footer.center(w)[:w - 1])
    stdscr.attroff(curses.color_pair(1))

    if status_msg:
        stdscr.addstr(1, 2, status_msg[:w - 4], curses.A_DIM)

    stdscr.refresh()


def get_print_queue(printer_name):
    """Returns a list of dicts: {id, user, size, date} for jobs queued on this printer."""
    jobs = []
    for line in run(["lpstat", "-o", printer_name]):
        parts = line.split()
        if not parts:
            continue
        job_id = parts[0]
        user = parts[1] if len(parts) > 1 else ""
        size = parts[2] if len(parts) > 2 else ""
        date = " ".join(parts[3:]) if len(parts) > 3 else ""
        jobs.append({"id": job_id, "user": user, "size": size, "date": date})
    return jobs


def set_default_printer(name):
    """Sets the user's default printer via `lpoptions -d`, which unlike
    `lpadmin -d` doesn't require root. Returns True on success."""
    try:
        result = subprocess.run(
            ["lpoptions", "-d", name], capture_output=True, text=True, timeout=3
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def unset_default_printer(name):
    """Removes the user-level default-printer designation for `name` by
    editing the 'Default <name> ...' line out of ~/.cups/lpoptions.
    (There's no direct `lpoptions` flag to clear the default, so this edits
    the file lpoptions itself writes to.) Returns True on success."""
    path = os.path.expanduser("~/.cups/lpoptions")
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Default" and parts[1] == name:
                continue
            new_lines.append(line)
        with open(path, "w") as f:
            f.writelines(new_lines)
        return True
    except OSError:
        return False


def add_printer_cmd(name, uri, description="", location=""):
    """Adds a new printer queue via `lpadmin`. Returns (success, message).
    Uses the generic driverless 'everywhere' model, which works for most
    modern network/IPP printers. Local/USB printers with vendor-specific
    PPDs may need a driver installed separately for full functionality."""
    cmd = ["lpadmin", "-p", name, "-v", uri, "-m", "everywhere", "-E"]
    if description:
        cmd += ["-D", description]
    if location:
        cmd += ["-L", location]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return True, f"{name} added."
        err = result.stderr.strip() or "lpadmin failed (are you root, or in the lpadmin group?)"
        return False, err
    except FileNotFoundError:
        return False, "lpadmin not found."
    except subprocess.TimeoutExpired:
        return False, "lpadmin timed out."


def input_dialog(stdscr, title, fields):
    """Blocking multi-field text entry modal.

    fields: list of {'key', 'label', 'required'} dicts.
    Navigate fields with Up/Down/Tab, edit text by typing, Backspace to
    delete. Enter on a field moves to the next one; Enter on the Submit
    button submits. Esc cancels.

    Returns a dict of key -> value on submit, or None on cancel.
    """
    h, w = stdscr.getmaxyx()
    n = len(fields)

    win_w = min(w - 4, 64)
    win_w = max(win_w, 44)
    win_h = min(h - 4, n * 2 + 6)
    win_h = max(win_h, n * 2 + 6)
    y = (h - win_h) // 2
    x = (w - win_w) // 2

    win = curses.newwin(win_h, win_w, y, x)
    win.keypad(True)

    values = ["" for _ in fields]
    focus = 0  # 0..n-1 = fields, n = submit button
    error_msg = ""

    while True:
        win.erase()
        win.box()
        win.addstr(0, 2, f" {title} ", curses.A_BOLD)

        row = 2
        input_positions = []
        for i, f in enumerate(fields):
            label = f["label"] + (" *" if f.get("required") else "") + ": "
            win.addstr(row, 2, label[: win_w - 4])
            input_x = 2 + len(label)
            max_val_w = max(4, win_w - input_x - 2)
            val = values[i]
            shown = val[-(max_val_w - 1):] if len(val) >= max_val_w else val
            attr = curses.A_UNDERLINE
            if i == focus:
                attr |= curses.A_BOLD
            win.addstr(row, input_x, shown.ljust(max_val_w)[:max_val_w], attr)
            input_positions.append((row, input_x + len(shown)))
            row += 2

        submit_row = row
        submit_label = " Add Printer "
        submit_attr = curses.A_REVERSE | curses.A_BOLD if focus == n else curses.A_NORMAL
        win.addstr(submit_row, 2, submit_label, submit_attr)

        if error_msg and submit_row + 1 < win_h - 1:
            win.addstr(submit_row + 1, 2, error_msg[: win_w - 4], curses.color_pair(3))

        hint = "Tab/Up/Down: move  Enter: next/submit  Esc: cancel"
        if win_h - 2 > submit_row:
            win.addstr(win_h - 2, 2, hint[: win_w - 4], curses.A_DIM)

        if focus < n:
            curses.curs_set(1)
            crow, ccol = input_positions[focus]
            try:
                win.move(crow, min(ccol, win_w - 2))
            except curses.error:
                pass
        else:
            curses.curs_set(0)

        win.refresh()

        key = win.getch()

        if key == 27:  # Esc
            curses.curs_set(0)
            return None
        elif key in (curses.KEY_UP,):
            focus = (focus - 1) % (n + 1)
            error_msg = ""
        elif key in (curses.KEY_DOWN, ord("\t")):
            focus = (focus + 1) % (n + 1)
            error_msg = ""
        elif key in (curses.KEY_ENTER, 10, 13):
            if focus == n:
                missing = [f["label"] for i, f in enumerate(fields)
                           if f.get("required") and not values[i].strip()]
                if missing:
                    error_msg = f"Required: {', '.join(missing)}"
                    continue
                curses.curs_set(0)
                return {fields[i]["key"]: values[i].strip() for i in range(n)}
            else:
                focus = min(focus + 1, n)
                error_msg = ""
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if focus < n:
                values[focus] = values[focus][:-1]
        elif 32 <= key <= 126:
            if focus < n:
                values[focus] += chr(key)



    """Blocking Yes/No modal navigated with the Left/Right arrow keys and
    confirmed with Enter. Returns True for yes, False for no/escape."""
    h, w = stdscr.getmaxyx()
    message_lines = [message]

    options = ["Yes", "No"]
    selected_idx = 0 if default_yes else 1  # default focus on "Yes"

    win_w = min(w - 4, max(34, max(len(l) for l in message_lines) + 4))
    win_h = min(h - 4, len(message_lines) + 5)  # + blank line + option row + borders
    win_h = max(win_h, 7)
    win_w = max(win_w, 34)
    y = (h - win_h) // 2
    x = (w - win_w) // 2

    win = curses.newwin(win_h, win_w, y, x)
    win.keypad(True)

    while True:
        win.erase()
        win.box()
        win.addstr(0, 2, " Confirm ", curses.A_BOLD)

        for i, line in enumerate(message_lines):
            row = 2 + i
            if row >= win_h - 3:
                break
            win.addstr(row, 2, line[: win_w - 4])

        # Render the Yes/No options as a highlighted button row, centered.
        labels = [f" {opt} " for opt in options]
        gap = "   "
        total_w = sum(len(l) for l in labels) + len(gap)
        start_x = max(2, (win_w - total_w) // 2)

        col = start_x
        option_row = win_h - 2
        for i, label in enumerate(labels):
            attr = curses.A_REVERSE | curses.A_BOLD if i == selected_idx else curses.A_NORMAL
            win.addstr(option_row, col, label, attr)
            col += len(label) + len(gap)

        win.refresh()

        key = win.getch()
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("\t")):
            selected_idx = 1 - selected_idx
        elif key in (curses.KEY_ENTER, 10, 13):
            return selected_idx == 0
        elif key == 27:  # Esc
            return False


def main(stdscr):
    curses.curs_set(0)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # header/footer
    curses.init_pair(2, curses.COLOR_GREEN, -1)                  # good state
    curses.init_pair(3, curses.COLOR_RED, -1)                    # bad state
    curses.init_pair(4, curses.COLOR_YELLOW, -1)                 # add-printer accent

    cups_available = shutil.which("lpstat") is not None

    # --- Shared state, updated by background threads so that lpstat calls
    # never block the keyboard-handling loop. ---
    state_lock = threading.Lock()
    state = {
        "printers": [],
        "default_printer": None,
        "status_msg": "Loading..." if cups_available else "",
        "version": 0,
        "queue": [],
        "queue_for": None,
        "queue_loading": False,
        "queue_version": 0,
        "selected_name": None,
        "selection_epoch": 0,
    }

    def fetch_printers():
        new_printers, new_default = get_printer_data() if cups_available else ([], None)
        with state_lock:
            state["printers"] = new_printers
            state["default_printer"] = new_default
            state["status_msg"] = f"Last updated: {time.strftime('%H:%M:%S')}"
            state["version"] += 1
            target = state["selected_name"]
            epoch = state["selection_epoch"]
        return target, epoch

    def fetch_queue(name, epoch):
        jobs = get_print_queue(name) if (cups_available and name) else []
        with state_lock:
            # Only apply this result if the selection hasn't moved on since
            # the fetch was kicked off (avoids stale data flashing in).
            if state["selection_epoch"] == epoch:
                state["queue"] = jobs
                state["queue_for"] = name
                state["queue_loading"] = False
                state["queue_version"] += 1

    force_refresh = threading.Event()
    stop_event = threading.Event()

    def poller():
        # Fetch immediately on startup, then wait between subsequent cycles.
        while not stop_event.is_set():
            if cups_available:
                target, epoch = fetch_printers()
                if target:
                    fetch_queue(target, epoch)
            force_refresh.wait(timeout=REFRESH_SECONDS)
            force_refresh.clear()

    poll_thread = threading.Thread(target=poller, daemon=True)
    poll_thread.start()

    def select_printer(name):
        """Update which printer's queue we're tracking and kick off a fast,
        one-off fetch for it in the background (decoupled from the main
        2s poll cycle so the queue updates immediately on selection change)."""
        with state_lock:
            if state["selected_name"] == name:
                return
            state["selected_name"] = name
            state["selection_epoch"] += 1
            state["queue_loading"] = True
            state["queue_version"] += 1
            epoch = state["selection_epoch"]
        threading.Thread(target=fetch_queue, args=(name, epoch), daemon=True).start()

    def make_default(name):
        """Sets `name` as the default printer in the background, then forces
        a full refresh so the '*' marker and any status message update."""
        with state_lock:
            state["status_msg"] = f"Setting {name} as default..."
            state["version"] += 1
        success = set_default_printer(name)
        with state_lock:
            state["status_msg"] = (
                f"{name} set as default." if success else f"Failed to set {name} as default."
            )
            state["version"] += 1
        force_refresh.set()

    def make_not_default(name):
        """Removes `name` as the default printer in the background, then
        forces a full refresh so the '*' marker and status message update."""
        with state_lock:
            state["status_msg"] = f"Removing {name} as default..."
            state["version"] += 1
        success = unset_default_printer(name)
        with state_lock:
            state["status_msg"] = (
                f"{name} is no longer the default." if success
                else f"Failed to remove {name} as default."
            )
            state["version"] += 1
        force_refresh.set()

    def add_printer(name, uri, description, location):
        with state_lock:
            state["status_msg"] = f"Adding {name}..."
            state["version"] += 1
        success, msg = add_printer_cmd(name, uri, description, location)
        with state_lock:
            state["status_msg"] = msg if success else f"Failed to add {name}: {msg}"
            state["version"] += 1
        force_refresh.set()

    selected = 0
    last_seen_version = -1
    last_seen_queue_version = -1

    def snapshot():
        with state_lock:
            return (
                list(state["printers"]),
                state["default_printer"],
                state["status_msg"],
                state["version"],
                list(state["queue"]),
                state["queue_for"],
                state["queue_loading"],
                state["queue_version"],
            )

    (printers, default_printer, status_msg, last_seen_version,
     queue, queue_for, queue_loading, last_seen_queue_version) = snapshot()
    draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
         loading=(cups_available and last_seen_version == 0),
         queue=queue, queue_for=queue_for, queue_loading=queue_loading)

    # Keep getch responsive (short timeout) regardless of the 2s poll cycle.
    stdscr.timeout(50)

    while True:
        (printers, default_printer, status_msg, version,
         queue, queue_for, queue_loading, queue_version) = snapshot()
        loading = cups_available and version == 0

        redraw_needed = version != last_seen_version or queue_version != last_seen_queue_version
        if version != last_seen_version:
            last_seen_version = version
            # Valid selection range is 0 (Add Printer) .. len(printers).
            if selected > len(printers):
                selected = len(printers)
        last_seen_queue_version = queue_version

        if selected > 0 and printers:
            select_printer(printers[selected - 1]["name"])
        else:
            select_printer(None)

        if redraw_needed:
            draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
                 loading=loading, queue=queue, queue_for=queue_for, queue_loading=queue_loading)

        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key in (ord("q"), ord("Q")):
            stop_event.set()
            force_refresh.set()
            break
        elif key in (ord("r"), ord("R")):
            force_refresh.set()
        elif key == curses.KEY_UP:
            selected = max(0, selected - 1)
            if selected > 0 and printers:
                select_printer(printers[selected - 1]["name"])
            else:
                select_printer(None)
            _, _, _, _, queue, queue_for, queue_loading, _ = snapshot()
            draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
                 loading=loading, queue=queue, queue_for=queue_for, queue_loading=queue_loading)
        elif key == curses.KEY_DOWN:
            selected = min(len(printers), selected + 1)
            if selected > 0 and printers:
                select_printer(printers[selected - 1]["name"])
            else:
                select_printer(None)
            _, _, _, _, queue, queue_for, queue_loading, _ = snapshot()
            draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
                 loading=loading, queue=queue, queue_for=queue_for, queue_loading=queue_loading)
        elif key in (curses.KEY_ENTER, 10, 13):
            if selected == 0:
                fields = [
                    {"key": "name", "label": "Printer Name", "required": True},
                    {"key": "uri", "label": "Device URI", "required": True},
                    {"key": "description", "label": "Description", "required": False},
                    {"key": "location", "label": "Location", "required": False},
                ]
                result = input_dialog(stdscr, "Add Printer", fields)
                if result:
                    name = result["name"].strip().replace(" ", "_")
                    uri = result["uri"].strip()
                    description = result["description"]
                    location = result["location"]
                    threading.Thread(
                        target=add_printer, args=(name, uri, description, location), daemon=True
                    ).start()
            elif printers:
                name = printers[selected - 1]["name"]
                is_default = name == default_printer
                if is_default:
                    confirmed = confirm_dialog(stdscr, f"Remove {name} as the default printer?")
                    if confirmed:
                        threading.Thread(target=make_not_default, args=(name,), daemon=True).start()
                else:
                    confirmed = confirm_dialog(stdscr, f"Set {name} as the default printer?")
                    if confirmed:
                        threading.Thread(target=make_default, args=(name,), daemon=True).start()
            # The dialog drew over the screen; redraw the normal view.
            draw(stdscr, printers, default_printer, selected, status_msg, cups_available,
                 loading=loading, queue=queue, queue_for=queue_for, queue_loading=queue_loading)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
