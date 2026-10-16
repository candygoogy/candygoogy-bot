"""The keyboard under the message box: its buttons and their commands."""

from telegram import KeyboardButton, ReplyKeyboardMarkup

# A tap sends the button's label as a message, which runs the same handler as
# typing the command, so Sleep/Reboot/Shut down still ask for confirmation first.
MENU = [
    [("wake", "⚡ Wake"), ("status", "📊 Status"), ("homelab", "🖥 Homelab")],
    [("sleep", "😴 Sleep"), ("reboot", "🔄 Reboot"), ("shutdown", "🔌 Shut down")],
    [("timers", "⏰ Timers"), ("logs", "📜 Logs"), ("about", "ℹ️ About")],
    [("net", "🌐 Internet"), ("summary", "🗓 Summary"), ("week", "📅 Week")],
    [("schedule_shutdown", "⏲ Shut down later"), ("schedule_wake", "⏲ Wake later")],
]
LABELS = [label for row in MENU for _, label in row]
KEYBOARD_COMMANDS = {label: name for row in MENU for name, label in row}

# Telegram keeps showing the keyboard of the latest message that carried one,
# until another replaces it. So /start, the startup message and every message
# sent unasked (alerts, the daily summary) carry it, which keeps it there even
# on a new device. Messages the bot edits later can't carry it.
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(label) for _, label in row] for row in MENU],
    resize_keyboard=True,  # rows as tall as the text, not half the screen
    is_persistent=True,  # shown even when the phone's own keyboard is closed
    input_field_placeholder="Tap a button, or type / for every command",
)
