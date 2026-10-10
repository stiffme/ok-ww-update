using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Windows.Forms;

namespace RdpHost;

internal static class Program
{
    // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    private static readonly IntPtr PerMonitorAwareV2 = new(-4);

    [DllImport("user32.dll")]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    [STAThread]
    private static void Main(string[] args)
    {
        var options = ParseArguments(args);
        // No console for a WinExe; without a pipe there is nothing to do.
        if (string.IsNullOrEmpty(options.PipeName))
        {
            return;
        }

        try
        {
            _ = SetProcessDpiAwarenessContext(PerMonitorAwareV2);
        }
        catch (Exception)
        {
            // Older Windows: keep default awareness.
        }

        var form = new RdpHostForm(options.Width, options.Height);
        var pipe = new PipeServer(options.PipeName, request => Dispatch(form, request));

        form.LoginCompleted += (_, _) => _ = pipe.SendEventAsync("login_complete");
        form.ConnectionFailed += (_, e) => _ = pipe.SendEventAsync("connection_failed", new JsonObject
        {
            ["message"] = e.Message,
            ["errorCode"] = e.ErrorCode,
            ["extendedErrorCode"] = e.ExtendedErrorCode,
        });
        form.RemoteDesktopSizeChanged += (_, size) => _ = pipe.SendEventAsync("remote_desktop_size_change", new JsonObject
        {
            ["width"] = size.Width,
            ["height"] = size.Height,
        });
        form.InputWindowFocusChanged += (_, focused) => _ = pipe.SendEventAsync("input_window_focus", new JsonObject
        {
            ["focused"] = focused,
        });
        form.CursorCapturedChanged += (_, captured) => _ = pipe.SendEventAsync("cursor_captured", new JsonObject
        {
            ["captured"] = captured,
        });

        form.Shown += (_, _) => pipe.Start();
        Application.Run(form);
        pipe.Dispose();
    }

    private static JsonObject Dispatch(RdpHostForm form, JsonObject request)
    {
        var op = request["op"]?.GetValue<string>() ?? string.Empty;
        var args = request["args"] as JsonObject ?? new JsonObject();
        // All RDP/ActiveX work must run on the UI thread that owns the control.
        var result = form.Invoke(new Func<JsonObject>(() => Invoke(form, op, args)));
        return (JsonObject)result;
    }

    private static JsonObject Invoke(RdpHostForm form, string op, JsonObject args)
    {
        switch (op)
        {
            case "hello":
                return new JsonObject
                {
                    ["helperPid"] = Environment.ProcessId,
                    ["sessionId"] = ChildSessionNativeMethods.TryGetChildSessionId() ?? 0,
                    ["hostHwnd"] = form.HostHandle.ToInt64(),
                    ["rdpPort"] = ChildSessionNativeMethods.GetConfiguredRdpPort(),
                    ["rdpWrapper"] = ChildSessionNativeMethods.IsRdpWrapperEnabled(),
                    ["version"] = Assembly.GetExecutingAssembly().GetName().Version?.ToString(),
                };

            case "connect":
                form.Connect(
                    GetInt(args, "width", 1920),
                    GetInt(args, "height", 1080),
                    GetBool(args, "smartSizing", true),
                    GetBool(args, "audioMuted", false),
                    GetBool(args, "sendSystemShortcuts", true));
                return new JsonObject();

            case "disconnect":
                form.Disconnect();
                return new JsonObject();

            case "reconnect":
            case "resize":
                form.Reconnect(GetInt(args, "width", 1920), GetInt(args, "height", 1080));
                return new JsonObject();

            case "set_smart_sizing":
                form.SetSmartSizing(GetBool(args, "enabled", true));
                return new JsonObject();

            case "set_audio_muted":
                form.SetAudioMuted(GetBool(args, "muted", false));
                return new JsonObject();

            case "set_send_system_shortcuts":
                form.SetSendSystemShortcuts(GetBool(args, "enabled", true));
                return new JsonObject();

            case "send_shortcut":
                switch (args["name"]?.GetValue<string>())
                {
                    case "win_d":
                        form.SendShowDesktopShortcut();
                        break;
                    case "win_tab":
                        form.SendTaskViewShortcut();
                        break;
                    default:
                        throw new InvalidOperationException("未知的快捷键名称。");
                }

                return new JsonObject();

            case "send_keys":
                form.SendKeys(ParseKeys(args));
                return new JsonObject();

            case "focus_input":
                return new JsonObject { ["focused"] = form.FocusInput() };

            case "query_state":
                return new JsonObject
                {
                    ["connected"] = form.ConnectedState,
                    ["sessionId"] = ChildSessionNativeMethods.TryGetChildSessionId() ?? 0,
                    ["inputFocused"] = form.InputFocused,
                };

            case "shutdown":
                if (GetBool(args, "logoff", false))
                {
                    form.LogoffChildSession();
                }
                else
                {
                    form.Disconnect();
                }

                // Respond first, then exit on the next message-pump cycle.
                form.BeginInvoke(new Action(() => form.Close()));
                return new JsonObject();

            default:
                throw new InvalidOperationException($"未知操作：{op}");
        }
    }

    private static (int Scan, bool Up, bool Extended)[] ParseKeys(JsonObject args)
    {
        if (args["keys"] is not JsonArray array)
        {
            return Array.Empty<(int, bool, bool)>();
        }

        var keys = new List<(int, bool, bool)>(array.Count);
        foreach (var node in array)
        {
            var key = node as JsonObject ?? throw new InvalidOperationException("按键项无效。");
            keys.Add((GetInt(key, "scan", 0), GetBool(key, "up", false), GetBool(key, "extended", false)));
        }

        return keys.ToArray();
    }

    private static int GetInt(JsonObject obj, string name, int fallback)
    {
        return obj[name] is JsonValue value && value.TryGetValue(out int result) ? result : fallback;
    }

    private static bool GetBool(JsonObject obj, string name, bool fallback)
    {
        return obj[name] is JsonValue value && value.TryGetValue(out bool result) ? result : fallback;
    }

    private sealed record Options(string? PipeName, int Width, int Height);

    private static Options ParseArguments(string[] args)
    {
        string? pipe = null;
        var width = 1920;
        var height = 1080;
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--pipe" when i + 1 < args.Length:
                    pipe = args[++i];
                    break;
                case "--host-width" when i + 1 < args.Length:
                    width = int.TryParse(args[++i], out var w) ? w : width;
                    break;
                case "--host-height" when i + 1 < args.Length:
                    height = int.TryParse(args[++i], out var h) ? h : height;
                    break;
            }
        }

        return new Options(pipe, width, height);
    }
}
