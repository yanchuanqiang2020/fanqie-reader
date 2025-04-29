<!-- views/SearchView.vue -->
<template>
  <div class="search-container">
    <h2 class="page-title">搜索小说</h2>

    <el-card>
      <div class="search-form">
        <el-input
          v-model="searchQuery"
          placeholder="输入小说名称或作者进行搜索"
          class="search-input"
          clearable
          :prefix-icon="Search"
          @keyup.enter="handleSearch"
        />
        <el-button type="primary" @click="handleSearch" :loading="searching">搜索</el-button>
      </div>

      <div class="search-tips" v-if="!hasSearched">
        <el-empty description="输入关键词开始搜索">
          <template #description>
            <p>您可以搜索小说名称或作者，找到您想要的小说</p>
            <p>搜索到小说后，可以点击"添加"按钮将其添加到您的书库</p>
          </template>
        </el-empty>
      </div>

      <div class="search-results" v-else>
        <div v-if="novelStore.isLoading" class="search-loading">
          <el-skeleton :rows="3" animated />
          <el-skeleton :rows="3" animated />
          <el-skeleton :rows="3" animated />
        </div>

        <div v-else-if="novelStore.searchResults.length === 0" class="no-results">
          <el-empty description="暂无搜索结果">
            <template #description>
              <p>找不到与"{{ lastSearchQuery }}"相关的小说</p>
              <p>请尝试其他关键词，或检查拼写是否正确</p>
            </template>
          </el-empty>
        </div>

        <div v-else>
          <h3>搜索结果：{{ novelStore.searchResults.length }} 个结果</h3>

          <el-table :data="novelStore.searchResults" style="width: 100%">
            <el-table-column prop="id" label="ID" width="80" />
            <el-table-column prop="title" label="标题" min-width="200">
              <template #default="{ row }">
                <div class="result-title">{{ row.title }}</div>
              </template>
            </el-table-column>
            <el-table-column prop="author" label="作者" min-width="150">
              <template #default="{ row }">
                {{ row.author || '未知' }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  type="primary"
                  @click="addNovel(row.id)"
                  :loading="addingNovelId === row.id"
                >
                  添加
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { useNovelStore, useAuthStore } from '../store'
import api from '../api'
import type { NovelSearchResult } from '../api'

const novelStore = useNovelStore()
const authStore = useAuthStore()

// 页面状态
const searchQuery = ref('')
const lastSearchQuery = ref('')
const hasSearched = ref(false)
const searching = ref(false)
const addingNovelId = ref<string | null>(null)
const socketConnected = computed(() => api.WebSocketAPI.isConnected())

// 处理搜索
const handleSearch = async () => {
  if (!searchQuery.value.trim()) {
    ElMessage.warning('请输入搜索关键词')
    return
  }

  searching.value = true
  lastSearchQuery.value = searchQuery.value

  try {
    await novelStore.searchNovels(searchQuery.value)
    hasSearched.value = true
  } finally {
    searching.value = false
  }
}

// 添加小说
const addNovel = async (novelId: string) => {
  addingNovelId.value = novelId

  try {
    const response = await novelStore.addNovel(novelId)

    if (response) {
      ElMessage.success('小说添加成功，开始下载')
    }
  } catch (err) {
    ElMessage.error('添加小说失败')
  } finally {
    addingNovelId.value = null
  }
}
</script>

<style scoped>
.search-container {
  padding-bottom: 20px;
}

.page-title {
  margin-bottom: 20px;
}

.search-form {
  display: flex;
  margin-bottom: 20px;
  gap: 10px;
}

.search-input {
  flex: 1;
}

.search-tips {
  margin: 40px 0;
}

.no-results {
  margin: 40px 0;
}

.result-title {
  font-weight: bold;
}

.search-loading {
  padding: 20px 0;
}
</style>
