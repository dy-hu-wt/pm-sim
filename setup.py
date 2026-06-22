from setuptools import find_packages, setup


setup(
    name="pm-sim",
    version="0.1.0",
    description="Local project-manager simulation environment.",
    packages=find_packages(),
    python_requires=">=3.9",
    extras_require={"llm": ["openai>=1.0.0"]},
    entry_points={"console_scripts": ["pm-sim=pm_sim.cli:main"]},
)
