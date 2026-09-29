package app.planks.workout;

import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.appcompat.app.c;

/* loaded from: classes.dex */
public final class PermissionsRationaleActivity extends c {

    public static final class a extends WebViewClient {
        a() {
        }

        @Override // android.webkit.WebViewClient
        public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest webResourceRequest) {
            return false;
        }
    }

    @Override // androidx.fragment.app.AbstractActivityC2940t, androidx.activity.j, androidx.core.app.i, android.app.Activity
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WebView webView = new WebView(this);
        webView.setWebViewClient(new a());
        webView.loadUrl("https://developer.android.com/health-and-fitness/guides/health-connect/develop/get-started");
        setContentView(webView);
    }
}
