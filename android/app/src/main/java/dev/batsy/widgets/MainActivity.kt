package dev.batsy.widgets

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import java.net.HttpURLConnection
import java.net.URL

/**
 * The app: Batsy's pages (Home, Claude, Computers, Music, Files), full screen. The pages live on the laptop
 * (batsy/phone_ui, served at /app), so they change there without reinstalling this. This only finds the laptop
 * (USB first, then Tailscale), hands the page the pairing token, and shows a retry screen when it's out of reach.
 * Pairing and the plain fallback screen are in SettingsActivity.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var offline: LinearLayout
    private lateinit var offlineText: TextView
    private var loadedBase = ""
    private var micAsk: PermissionRequest? = null  // the page wants the mic: waiting for Android's permission

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        if (intent.getBooleanExtra("preview", false)) {  // the widget preview lives with the old screen
            startActivity(Intent(this, SettingsActivity::class.java).putExtras(intent)); finish(); return
        }
        if (!Hub.paired(this)) {
            startActivity(Intent(this, SettingsActivity::class.java)); finish(); return
        }
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)  // a dashboard: left open
        window.statusBarColor = BG
        window.navigationBarColor = BG
        web = WebView(this).apply {
            setBackgroundColor(BG)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            addJavascriptInterface(Bridge(), "BatsyApp")
            webChromeClient = object : WebChromeClient() {
                // the page's mic (hold to talk / phone as Batsy's ears): allowed once Android allows this app the mic
                override fun onPermissionRequest(request: PermissionRequest) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE !in request.resources) { request.deny(); return }
                    if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
                        android.content.pm.PackageManager.PERMISSION_GRANTED) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        micAsk = request
                        requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), MIC)
                    }
                }
            }
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(v: WebView, r: WebResourceRequest): Boolean {
                    if (r.url.toString().startsWith(loadedBase)) return false
                    startActivity(Intent(Intent.ACTION_VIEW, r.url)); return true  // links open in the browser
                }
                override fun onReceivedError(v: WebView, r: WebResourceRequest, e: WebResourceError) {
                    if (r.isForMainFrame) showOffline("Lost the laptop (${e.description}).")
                }
            }
        }
        offlineText = TextView(this).apply {
            setTextColor(Color.parseColor("#8D95A5")); textSize = 16f; gravity = Gravity.CENTER
            setPadding(0, 0, 0, dp(18))
        }
        offline = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER; setBackgroundColor(BG)
            setPadding(dp(32), 0, dp(32), 0); visibility = View.GONE
            addView(TextView(this@MainActivity).apply {
                text = "Batsy"; setTextColor(Color.parseColor("#EEE8DA")); textSize = 34f; gravity = Gravity.CENTER
            })
            addView(offlineText)
            addView(Button(this@MainActivity).apply { text = "Try again"; setOnClickListener { connect() } })
            addView(Button(this@MainActivity).apply { text = "Settings"; setOnClickListener { openSettings() } })
        }
        setContentView(FrameLayout(this).apply { addView(web); addView(offline) })
        fullScreen()
        connect()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, perms, results)
        if (code != MIC) return
        val ask = micAsk ?: return
        micAsk = null
        if (results.firstOrNull() == android.content.pm.PackageManager.PERMISSION_GRANTED) {
            ask.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
        } else ask.deny()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) fullScreen()  // the bars come back after a swipe from the edge: hide them again
    }

    /** No status bar, no navigation bar: StandBy is the whole screen. A swipe from the edge shows them briefly. */
    @Suppress("DEPRECATION")
    private fun fullScreen() {
        if (android.os.Build.VERSION.SDK_INT >= 30) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(android.view.WindowInsets.Type.systemBars())
                it.systemBarsBehavior = android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION)
        }
        if (android.os.Build.VERSION.SDK_INT >= 28) window.attributes = window.attributes.apply {
            layoutInDisplayCutoutMode = android.view.WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized && offline.visibility == View.VISIBLE) connect()
    }

    /** USB first (instant when plugged in), then the laptop's Tailscale address. */
    private fun connect() {
        offline.visibility = View.GONE
        Thread {
            val routes = mutableListOf(Hub.USB to "USB")
            Hub.remote(this).takeIf { it.isNotEmpty() && it != Hub.USB }?.let { routes += it to "Tailscale" }
            val found = routes.firstOrNull { (base, _) -> reachable(base, if (base == Hub.USB) 1500 else 5000) }
            runOnUiThread {
                if (isFinishing) return@runOnUiThread
                if (found == null) {
                    showOffline("Can't reach the laptop.\nPlug the phone into it, or turn on Tailscale on the phone.")
                } else {
                    loadedBase = found.first
                    val ask = if (intent.getBooleanExtra("ask", false)) "&focus=ask" else ""
                    web.loadUrl("${found.first}/app/#t=${Uri.encode(Hub.token(this))}&via=${found.second}$ask")
                }
            }
        }.start()
    }

    private fun reachable(base: String, timeoutMs: Int): Boolean = try {
        val c = URL("$base/ping").openConnection() as HttpURLConnection
        c.connectTimeout = timeoutMs; c.readTimeout = timeoutMs
        val ok = c.responseCode == 200
        c.disconnect(); ok
    } catch (e: Exception) { false }

    private fun showOffline(text: String) {
        offlineText.text = text
        offline.visibility = View.VISIBLE
    }

    private fun openSettings() = startActivity(Intent(this, SettingsActivity::class.java))

    @Deprecated("Back closes the app; the pages have no history of their own")
    override fun onBackPressed() = finish()

    /** What the pages may ask of the app. */
    inner class Bridge {
        @JavascriptInterface fun settings() = runOnUiThread { openSettings() }
        @JavascriptInterface fun refreshWidgets() = Refresh.now(this@MainActivity)
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object { val BG = Color.BLACK; const val MIC = 7 }
}
