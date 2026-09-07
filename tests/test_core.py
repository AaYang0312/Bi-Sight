"""核心离线检查：配置、快麦接口边界、指标输入、模型回合、预算假设与对话。"""

import unittest


class ConfigTests(unittest.TestCase):
    def test_selected_provider_uses_its_own_key(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "deepseek",
            "LLM_MODEL": "demo-model",
            "DEEPSEEK_API_KEY": "fake-deepseek-key",
            "QWEN_API_KEY": "fake-qwen-key",
        }
        settings = load_model_settings(env)
        self.assertEqual(settings.api_key.get_secret_value(), "fake-deepseek-key")
        with self.assertRaises(ValueError):
            load_model_settings({**env, "LLM_PROVIDER": "unknown"})
        self.assertNotIn("fake-deepseek-key", repr(settings))

    def test_missing_model_id_rejected(self):
        from bi_agent.config import load_model_settings

        env = {"LLM_PROVIDER": "qwen", "LLM_MODEL": "", "QWEN_API_KEY": "k"}
        with self.assertRaises(ValueError):
            load_model_settings(env)

    def test_base_url_must_be_https(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "m",
            "QWEN_API_KEY": "k",
            "LLM_BASE_URL": "http://insecure.example.com/v1",
        }
        with self.assertRaises(ValueError):
            load_model_settings(env)


if __name__ == "__main__":
    unittest.main()
