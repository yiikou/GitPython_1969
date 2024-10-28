.PHONY: all clean release force_release

all:
	@awk -F: '/^[[:alpha:]].*:/ && !/^all:/ {print $$1}' Makefile

clean:
	rm -rf build/ dist/ .eggs/ .tox/

release: clean
	./check-version.sh
	make force_release

force_release: clean
	./build-release.sh
	twine upload dist/*
	git push --tags origin main
