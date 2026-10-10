using System;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace RdpHost;

/// <summary>
/// VM-style cursor capture for the embedded RDP view.
///
/// Clicking inside the connected desktop traps the physical cursor within the view
/// (ClipCursor) and hides it (Cursor.Hide) so the remote cursor becomes the only one
/// visible; pressing Alt releases the trap and returns the cursor to the host.
///
/// Global low-level hooks are required because the RDP ActiveX control swallows the raw
/// mouse/keyboard messages through its own "Input Capture Window" and (with
/// KeyboardHookMode enabled) its own low-level keyboard hook, so WinForms-level events
/// never see them.
/// </summary>
internal sealed class CursorCapture : IDisposable
{
    private const int WhMouseLl = 14;
    private const int WhKeyboardLl = 13;
    private const int WmLButtonDown = 0x0201;
    private const int WmKeyDown = 0x0100;
    private const int WmKeyUp = 0x0101;
    private const int WmSysKeyDown = 0x0104;
    private const int WmSysKeyUp = 0x0105;
    private const int VkMenu = 0x12;
    private const int VkLMenu = 0xA4;
    private const int VkRMenu = 0xA5;

    private readonly RdpHostForm _form;
    private readonly LowLevelMouseProc _mouseProc;
    private readonly LowLevelKeyboardProc _keyboardProc;
    private IntPtr _mouseHook;
    private IntPtr _keyboardHook;
    private bool _captured;
    private bool _swallowAltUp;

    internal CursorCapture(RdpHostForm form)
    {
        _form = form;
        _mouseProc = OnMouse;
        _keyboardProc = OnKeyboard;
        _mouseHook = SetWindowsHookEx(WhMouseLl, _mouseProc, IntPtr.Zero, 0);
        // The keyboard hook is installed lazily on first grab (see InstallKeyboardHook)
        // so that it is registered after the RDP client's own keyboard hook and therefore
        // runs first in the chain, letting us swallow the Alt release key.
    }

    /// <summary>Raised on the UI thread whenever the captured state changes.</summary>
    internal event EventHandler? CapturedChanged;

    internal bool Captured => _captured;

    internal void Grab()
    {
        if (_captured)
        {
            return;
        }

        _captured = true;
        InstallKeyboardHook();
        var clipRect = ToRect(_form.RectangleToScreen(_form.ClientRectangle));
        _ = ClipCursor(ref clipRect);
        Cursor.Hide();
        CapturedChanged?.Invoke(this, EventArgs.Empty);
    }

    internal void Release()
    {
        if (!_captured)
        {
            return;
        }

        _captured = false;
        _ = ClipCursor(IntPtr.Zero);
        Cursor.Show();
        CapturedChanged?.Invoke(this, EventArgs.Empty);
    }

    public void Dispose()
    {
        Release();
        UninstallKeyboardHook();
        if (_mouseHook != IntPtr.Zero)
        {
            _ = UnhookWindowsHookEx(_mouseHook);
            _mouseHook = IntPtr.Zero;
        }
    }

    private void InstallKeyboardHook()
    {
        if (_keyboardHook != IntPtr.Zero)
        {
            _ = UnhookWindowsHookEx(_keyboardHook);
        }

        _keyboardHook = SetWindowsHookEx(WhKeyboardLl, _keyboardProc, IntPtr.Zero, 0);
    }

    private void UninstallKeyboardHook()
    {
        if (_keyboardHook != IntPtr.Zero)
        {
            _ = UnhookWindowsHookEx(_keyboardHook);
            _keyboardHook = IntPtr.Zero;
        }

        _swallowAltUp = false;
    }

    private IntPtr OnMouse(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && wParam == WmLButtonDown && !_captured && IsConnected())
        {
            var hookStruct = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(lParam);
            var rect = _form.RectangleToScreen(_form.ClientRectangle);
            if (hookStruct.pt.x >= rect.Left && hookStruct.pt.x < rect.Right
                && hookStruct.pt.y >= rect.Top && hookStruct.pt.y < rect.Bottom)
            {
                Grab();
            }
        }

        return CallNextHookEx(_mouseHook, nCode, wParam, lParam);
    }

    private IntPtr OnKeyboard(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0)
        {
            var hookStruct = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(lParam);
            if (IsAlt(hookStruct.vkCode))
            {
                if (wParam == WmKeyDown || wParam == WmSysKeyDown)
                {
                    if (_captured)
                    {
                        Release();
                        _swallowAltUp = true;
                        return (IntPtr)1;
                    }
                }
                else if (wParam == WmKeyUp || wParam == WmSysKeyUp)
                {
                    if (_swallowAltUp)
                    {
                        _swallowAltUp = false;
                        return (IntPtr)1;
                    }
                }
            }
        }

        return CallNextHookEx(_keyboardHook, nCode, wParam, lParam);
    }

    private bool IsConnected()
    {
        try
        {
            return _form.ConnectedState == 1;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool IsAlt(uint vkCode) => vkCode == VkMenu || vkCode == VkLMenu || vkCode == VkRMenu;

    private static RECT ToRect(Rectangle rect) => new()
    {
        Left = rect.Left,
        Top = rect.Top,
        Right = rect.Right,
        Bottom = rect.Bottom,
    };

    private delegate IntPtr LowLevelMouseProc(int nCode, IntPtr wParam, IntPtr lParam);

    private delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSLLHOOKSTRUCT
    {
        public POINT pt;
        public uint mouseData;
        public uint flags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KBDLLHOOKSTRUCT
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelMouseProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelKeyboardProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ClipCursor(ref RECT lpRect);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ClipCursor(IntPtr lpRect);
}
