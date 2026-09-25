// ugo.exe — bootstrap launcher di Ugo (compilato da boot/ugo_bootstrap.cs con csc.exe)
//
// Perché esiste: winget vuole un eseguibile come installer, mentre Ugo è un
// pacchetto Python. Questo exe da ~8 KB (niente admin, niente registry) fa da
// ponte: al primo avvio garantisce un Python 3.10+ (con winget come ultima
// spiaggia), installa il pacchetto ugo-agent via pip da GitHub e rilancia
// `ugo run`. Gli avvii successivi risolvono tutto in ~30 ms; gli aggiornamenti
// restano gestiti dal pacchetto stesso (`ugo update`).
//
// Compilazione: vedi packaging/Makefile
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class UgoBootstrap
{
    private const string RepoUrl =
        "https://github.com/giovannimazza/ugo-local-ai-agent.git";
    private const string WingetPython =
        "install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements";

    [DllImport("kernel32.dll")]
    private static extern bool AttachConsole(uint dwProcessId);

    [DllImport("user32.dll")]
    private static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    private sealed class ProcResult
    {
        public int ExitCode;
        public string Output = "";
    }

    private static int Main(string[] args)
    {
        try { Console.OutputEncoding = Encoding.UTF8; } catch { }

        // L'exe e' subsystem GUI (cosi' il doppio click non apre una finestra
        // dos vuota). Se pero' esiste una console padre (lancio da terminale,
        // scorciatoia, alias winget) ci ci attacca: l'output di setup/doctor/
        // update resta visibile. Doppio click / Win+R: nessuna console,
        // il child parte nascosto come ugo_app.py.
        bool console = AttachConsole(0xFFFFFFFFu /* ATTACH_PARENT_PROCESS */);

        string py = FindPython();
        if (py == null) return FailNoPython();
        if (!EnsurePackage(py, console)) return 1;

        var psi = new ProcessStartInfo();
        psi.FileName = py;
        psi.Arguments = Quote("-m") + " " + Quote("ugo_agent.cli") +
                        (args.Length > 0 ? " " + JoinQuoted(args) : "");
        psi.UseShellExecute = false;
        if (!console)
        {
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
        }
        Process p;
        try { p = Process.Start(psi); }
        catch (Exception exc)
        {
            Console.Error.WriteLine("[ugo] impossibile avviare " + py + " (" + exc.Message + ")");
            return 1;
        }
        p.WaitForExit();
        return p.ExitCode;
    }

    // ------------------------------------------------------------------
    // Python 3.10+
    // ------------------------------------------------------------------
    private static string FindPython()
    {
        // 1. python nel PATH: e' dove pip di sistema ha gia' installato le
        //    dipendenze su una macchina dove Ugo esiste gia'
        string pyPath = TryRun("python", "-c \"import sys;print(sys.executable)\"");
        if (pyPath != null && !TooOld(pyPath)) return pyPath;
        // 2. py launcher: tra i Python installati sceglie il primo versione
        //    supportata (3.13/3.12/...); Ugo dipende da wheel non ancora
        //    pronte per 3.14, quindi le versioni si sondano in ordine.
        foreach (string v in new[] { "-3.13", "-3.12", "-3.11", "-3.10" })
        {
            string p = TryRun("py", v + " -c \"import sys;print(sys.executable)\"");
            if (p != null) return p;
        }
        // 3. py launcher default (qualsiasi versione)
        string any = TryRun("py", "-c \"import sys;print(sys.executable)\"");
        if (any != null) return any;
        // 4. ultima spiaggia: winget installa Python (lo dichiara anche il
        //    manifest come PackageDependency, ma con i manifest locali la
        //    dependency non è automatica: la facciamo qui)
        Console.WriteLine("[ugo] Python non trovato: lo installo con winget (~1 min)...");
        try
        {
            var psi = new ProcessStartInfo("winget", WingetPython);
            psi.UseShellExecute = false;
            Process wp = Process.Start(psi);
            wp.WaitForExit();
        }
        catch { /* winget assente: si cade nel messaggio finale */ }
        string p2 = TryRun("py", "-3.12 -c \"import sys;print(sys.executable)\"");
        if (p2 != null) return p2;
        p2 = TryRun("python", "-c \"import sys;print(sys.executable)\"");
        if (p2 != null && !TooOld(p2)) return p2;
        return null;
    }

    private static bool TooOld(string pyExe)
    {
        string v = TryRun(pyExe, "-c \"import sys;print('%d.%d' % sys.version_info[:2])\"");
        if (v == null) return true;
        string[] parts = v.Split('.');
        int major, minor;
        if (parts.Length < 2
            || !int.TryParse(parts[0], out major)
            || !int.TryParse(parts[1], out minor)) return true;
        return major < 3 || (major == 3 && minor < 10);
    }

    private static int FailNoPython()
    {
        Console.Error.WriteLine("[ugo] Python 3.10+ non trovato e winget non disponibile.");
        Console.Error.WriteLine("      Installa Python da https://www.python.org/downloads/ ");
        Console.Error.WriteLine("      (o: winget install Python.Python.3.12) e rilancia ugo.");
        return 1;
    }

    // ------------------------------------------------------------------
    // Pacchetto ugo-agent
    // ------------------------------------------------------------------
    private static bool EnsurePackage(string py, bool console)
    {
        // importabile? il probe costa ~50 ms e rende i lanci successivi istantanei
        ProcResult probe = Run(py, "-c \"import ugo_agent\"");
        if (probe.ExitCode == 0) return true;

        if (!console)
            MessageBoxW(IntPtr.Zero,
                "Prima installazione di Ugo in corso (1-2 minuti: pacchetto da GitHub).\n" +
                "Al termine l'assistente si avvia da solo; i modelli (Whisper, Qwen)\n" +
                "vengono scaricati al primo 'ugo run'.",
                "Ugo", 0);

        Console.WriteLine("[ugo] Prima installazione: scarico ugo-agent da GitHub (1-2 min)...");
        ProcResult r = Run(py, "-m pip install --upgrade git+" + RepoUrl);
        if (r.ExitCode != 0)
        {
            if (!console)
                MessageBoxW(IntPtr.Zero,
                    "Installazione fallita: controlla la connessione e rilancia ugo.\n\n" +
                    "Manuale:\n\"" + Path.GetFileName(py) + "\" -m pip install git+" + RepoUrl,
                    "Ugo", 0x10 /* MB_ICONERROR */);
            Console.Error.WriteLine("[ugo] Installazione fallita: controlla la connessione e riprova.");
            Console.Error.WriteLine("       Manuale: " + Path.GetFileName(py) +
                                    " -m pip install git+" + RepoUrl);
            return false;
        }
        Console.WriteLine("[ugo] Pacchetto installato. Al primo 'ugo run' scarica i modelli");
        Console.WriteLine("      (Whisper, Qwen): servono alcuni minuti, poi tutto e' locale.");
        return true;
    }

    // ------------------------------------------------------------------
    // helper processo
    // ------------------------------------------------------------------
    private static ProcResult Run(string exe, string args)
    {
        var res = new ProcResult();
        try
        {
            var psi = new ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            Process p = Process.Start(psi);
            // lettura asincrona di entrambi gli stream: evita deadlock sui pipe
            var so = p.StandardOutput.ReadToEndAsync();
            var se = p.StandardError.ReadToEndAsync();
            p.WaitForExit();
            res.ExitCode = p.ExitCode;
            res.Output = (so.Result ?? "") + (se.Result ?? "");
        }
        catch (Exception exc)
        {
            res.ExitCode = -1;
            res.Output = exc.Message;
        }
        return res;
    }

    private static string TryRun(string exe, string args)
    {
        ProcResult r = Run(exe, args);
        if (r.ExitCode != 0) return null;
        string line = r.Output.Trim().Split('\n')[0].Trim().TrimEnd('\r');
        return (line.Length > 0 && File.Exists(line)) ? line : null;
    }

    private static string Quote(string s)
    {
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }

    private static string JoinQuoted(string[] args)
    {
        var sb = new StringBuilder();
        foreach (string a in args)
        {
            if (sb.Length > 0) sb.Append(' ');
            sb.Append(Quote(a));
        }
        return sb.ToString();
    }
}
