# VS Code Clone - Capacitor + GitHub APK

## 1. Push to GitHub
```bash
git init
git add .
git commit -m "vscode clone capacitor"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/vscode-clone.git
git push -u origin main
```

## 2. Download APK
GitHub -> Actions -> Build APK -> Artifacts -> vscode-clone-apk

## 3. Local build (Android Studio)
```
npm install
npx cap sync android
npx cap open android
# In Android Studio: Build > Build APK(s)
```

## Config
- App ID: com.vscodeclone.app
- App Name: VS Code Clone
- webDir: www (your clone is in www/index.html)
- minSdk 24, targetSdk 34
