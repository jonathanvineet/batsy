package dev.batsy.widgets

import android.content.Context
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Talking to Batsy on the laptop. Two routes, tried in order:
 *  - USB: the phone is plugged into the PC, which forwards 127.0.0.1:8765 to the laptop (Batsy sets that up).
 *  - Remote: the laptop's Tailscale address, from anywhere.
 * The last status is kept, so widgets draw instantly and still show something when Batsy can't be reached.
 */
object Hub {
    const val USB = "http://127.0.0.1:8765"
    private const val PREFS = "batsy"

    data class Result(val status: JSONObject?, val route: String, val error: String?)

    private fun prefs(c: Context) = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun token(c: Context): String = prefs(c).getString("token", "") ?: ""
    fun remote(c: Context): String = prefs(c).getString("remote", "") ?: ""
    fun paired(c: Context) = token(c).isNotEmpty()

    fun save(c: Context, token: String?, remote: String?) {
        prefs(c).edit().apply {
            if (!token.isNullOrBlank()) putString("token", token.trim())
            if (remote != null) putString("remote", remote.trim().trimEnd('/'))
        }.apply()
    }

    fun cached(c: Context): Result {
        val p = prefs(c)
        val raw = p.getString("last", null) ?: return Result(null, "", "Not connected yet")
        return Result(runCatching { JSONObject(raw) }.getOrNull(), p.getString("route", "") ?: "", "cached")
    }

    /** Fetch the status: USB first (fast to fail when not plugged in), then the remote address. */
    fun fetch(c: Context): Result {
        if (!paired(c)) return Result(null, "", "Open the Batsy app to pair")
        var lastError = "Batsy isn't reachable"
        for ((base, route, timeout) in routes(c)) {
            try {
                val json = JSONObject(request(c, base, "GET", "/api/status", null, timeout))
                prefs(c).edit().putString("last", json.toString()).putString("route", route)
                    .putLong("ok_at", System.currentTimeMillis()).apply()
                return Result(json, route, null)
            } catch (e: Unauthorized) {
                lastError = "Pairing expired: open the app and pair again"
            } catch (e: Exception) {
                lastError = if (route == "USB") "Batsy isn't reachable" else "Batsy isn't reachable (${e.javaClass.simpleName})"
            }
        }
        val cached = cached(c)
        return Result(cached.status, "", lastError)
    }

    /** A button: Spotify, stop, wake the PC, or a typed request. Returns what Batsy said. */
    fun post(c: Context, path: String, body: JSONObject, timeoutMs: Int = 8000): String {
        var error = "Batsy isn't reachable"
        for ((base, _, quick) in routes(c)) {
            try {
                val reply = JSONObject(request(c, base, "POST", path, body.toString(), maxOf(quick, timeoutMs)))
                return reply.optString("say", "Done.")
            } catch (e: Unauthorized) {
                return "Not paired any more: open the app and pair again."
            } catch (e: Exception) {
                error = "Batsy isn't reachable"
            }
        }
        return error
    }

    /** Exchange the code `batsy phone` shows for a token. */
    fun pair(c: Context, address: String, code: String): String? {
        val base = normalise(address)
        return try {
            val r = JSONObject(request(c, base, "POST", "/api/pair", JSONObject().put("code", code).toString(), 6000, auth = false))
            val remote = r.optString("remote").ifBlank { if (base == USB) "" else base }
            save(c, r.getString("token"), remote)
            null
        } catch (e: Exception) {
            if (e is Unauthorized || e.message?.contains("403") == true) "Wrong or expired code" else "Can't reach $base"
        }
    }

    fun normalise(address: String): String {
        var a = address.trim().trimEnd('/')
        if (a.isEmpty()) return USB
        if (!a.startsWith("http")) a = "http://$a"
        if (!Regex(":\\d+$").containsMatchIn(a.substringAfter("://"))) a = "$a:8765"
        return a
    }

    private fun routes(c: Context): List<Triple<String, String, Int>> {
        val list = mutableListOf(Triple(USB, "USB", 1500))
        val r = remote(c)
        if (r.isNotEmpty() && r != USB) list += Triple(r, "Tailscale", 5000)
        return list
    }

    class Unauthorized : Exception()

    private fun request(c: Context, base: String, method: String, path: String, body: String?, timeoutMs: Int,
                        auth: Boolean = true): String {
        val conn = URL(base + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = minOf(timeoutMs, 3000)
            conn.readTimeout = timeoutMs
            if (auth) conn.setRequestProperty("Authorization", "Bearer ${token(c)}")
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            if (code == 401 || code == 403) throw Unauthorized()
            if (code >= 400) throw RuntimeException("HTTP $code")
            return conn.inputStream.bufferedReader().use { it.readText() }
        } finally {
            conn.disconnect()
        }
    }
}
