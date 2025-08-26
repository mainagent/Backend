import inspect
import openai
import openai._client as c
import openai._base_client as b

print("openai version:", openai.__version__)
print("openai loaded from:", openai.__file__)
print("OpenAI.__init__ signature:", inspect.signature(c.OpenAI.__init__))
print("BaseClient.__init__ signature:", inspect.signature(b.BaseClient.__init__))