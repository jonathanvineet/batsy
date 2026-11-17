plugins {
    id("com.android.application")
}

android {
    namespace = "dev.batsy.widgets"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.batsy.widgets"
        minSdk = 26          // Android 8; the Galaxy A50 runs Android 11
        targetSdk = 36
        versionCode = 4
        versionName = "4.0"       // + the microphone (hold to talk, phone as Batsy's ears)
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")  // sideloaded, not from the Play Store
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
