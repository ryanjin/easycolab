package app.easycolab.studio

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class StudioSecrets(context: Context) {
    private val store = context.getSharedPreferences("studio_secrets", Context.MODE_PRIVATE)
    private val legacy = context.getSharedPreferences("easycolab_prefs", Context.MODE_PRIVATE)
    private val alias = "app.easycolab.studio.credentials.v1"

    companion object {
        private val keyLock = Any()
    }

    private fun key(): SecretKey = synchronized(keyLock) {
        val keys = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keys.getKey(alias, null) as? SecretKey)?.let { return@synchronized it }
        val spec = KeyGenParameterSpec.Builder(alias,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .build()
        KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(spec)
            generateKey()
        }
    }

    fun read(name: String): String {
        val saved = store.getString(name, null)
        if (saved != null) {
            return runCatching {
                val bytes = Base64.decode(saved, Base64.NO_WRAP)
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes, 0, 12))
                cipher.updateAAD(name.toByteArray(Charsets.UTF_8))
                String(cipher.doFinal(bytes, 12, bytes.size - 12), Charsets.UTF_8)
            }.getOrDefault("")
        }
        val old = legacy.getString(name, null) ?: return ""
        if (write(name, old)) legacy.edit().remove(name).commit()
        return old
    }

    fun migrateLegacyFile(file: File) {
        if (!file.isFile) return
        if (!store.contains(file.name)) {
            check(write(file.name, file.readText())) { "Unable to secure saved credentials" }
        }
        check(file.delete()) { "Unable to remove migrated plaintext credentials" }
    }

    fun save(name: String, value: String) {
        check(write(name, value)) { "Unable to update secure credential storage" }
    }

    fun write(name: String, value: String): Boolean = runCatching {
        if (value.isEmpty()) {
            store.edit().remove(name).commit()
        } else {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, key())
            cipher.updateAAD(name.toByteArray(Charsets.UTF_8))
            val blob = cipher.iv + cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            store.edit().putString(name, Base64.encodeToString(blob, Base64.NO_WRAP)).commit()
        }
    }.getOrDefault(false)
}
