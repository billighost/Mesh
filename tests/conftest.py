import os
from hypothesis import settings

settings.register_profile("ci", max_examples=2000, deadline=None)
settings.register_profile("default", max_examples=250)
if "HYPOTHESIS_PROFILE" in os.environ:
    settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])
