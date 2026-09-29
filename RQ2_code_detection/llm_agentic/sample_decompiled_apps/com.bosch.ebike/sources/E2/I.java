package E2;

import android.R;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.text.TextUtils;
import androidx.browser.customtabs.b;
import com.skobbler.ngx.BuildConfig;
import java.net.MalformedURLException;
import java.net.URL;
import java.util.Locale;
import l2.C1817b;

/* loaded from: classes.dex */
public abstract class I {

    private static final String f662a = "I";

    public static String g(Context context) {
        return n(context, "/documents/archive/current/PrivacyPolicy");
    }

    public static String l(Context context) {
        return n(context, "/documents/archive/current/TermsConditions");
    }

    private static String n(Context context, String str) {
        try {
            URL url = new URL(T.O(context));
            URL url2 = new URL(url.getProtocol(), url.getHost(), BuildConfig.BUILD_INFORMATION);
            String language = Locale.getDefault().getLanguage();
            String country = Locale.getDefault().getCountry();
            String url3 = url2.toString();
            Locale locale = Locale.ROOT;
            return String.format("%s%s_%s-%s.pdf", url3, str, language.toLowerCase(locale), country.toLowerCase(locale));
        } catch (MalformedURLException unused) {
            return "https://www.ebike-connect.com";
        }
    }

    public static void p(Context context, String str) {
        if (context == null || TextUtils.isEmpty(str)) {
            return;
        }
        b.C0158b c0158b = new b.C0158b();
        c0158b.g(context.getResources().getColor(com.bosch.ebike.app.common.h.f14922e));
        c0158b.d(context.getResources().getColor(com.bosch.ebike.app.common.h.f14919b));
        c0158b.f(context, R.anim.fade_in, R.anim.fade_out);
        c0158b.c(context, R.anim.fade_in, R.anim.fade_out);
        androidx.browser.customtabs.b a8 = c0158b.a();
        a8.f8680a.addFlags(1073741824);
        a8.f8680a.addFlags(268435456);
        a8.a(context, Uri.parse(str));
    }
}
