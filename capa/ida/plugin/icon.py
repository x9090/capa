# Copyright (C) 2020 Mandiant, Inc. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
# You may obtain a copy of the License at: [package root]/LICENSE.txt
# Unless required by applicable law or agreed to in writing, software distributed under the License
#  is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.
import base64

# this is just `capa/.github/icon.png`.
# embed it in source so we don't have to figure out how to package into pypi release.
ICON = base64.b64decode(
    "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"
)
