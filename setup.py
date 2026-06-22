from setuptools import find_packages, setup


setup(
    name="pm-sim",
    version="0.1.0",
    description="Local project-manager simulation environment.",
    packages=find_packages(),
    python_requires=">=3.9",
    entry_points={"console_scripts": ["pm-sim=pm_sim.cli:main"]},
)
