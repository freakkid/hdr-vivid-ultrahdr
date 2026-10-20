@rem Gradle Wrapper launcher (minimal, ASCII-only for cmd compatibility)
@echo off
setlocal

set DIRNAME=%~dp0
if "%DIRNAME%"=="" set DIRNAME=.
set CLASSPATH=%DIRNAME%gradle\wrapper\gradle-wrapper.jar

if defined JAVA_HOME (
  set JAVA_EXE=%JAVA_HOME%\bin\java.exe
) else (
  set JAVA_EXE=java.exe
)

"%JAVA_EXE%" -Dorg.gradle.appname=gradlew -classpath "%CLASSPATH%" org.gradle.wrapper.GradleWrapperMain %*
if errorlevel 1 (
  echo.
  echo [ERROR] Build failed. Check that JDK 17 and Android SDK 34 are installed.
  exit /b 1
)
endlocal
