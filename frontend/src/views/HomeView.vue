<!-- views/HomeView.vue -->
<template>
  <div class="home-container">
    <el-container>
      <el-header class="main-header">
        <div class="logo">
          <h1>小说管理系统</h1>
        </div>
        <div class="user-info">
          <template v-if="authStore.isAuthenticated">
            <el-dropdown @command="handleCommand">
              <span class="user-dropdown">
                {{ authStore.user?.username }}
                <el-icon><arrow-down /></el-icon>
              </span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="logout">退出登录</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
          <template v-else>
            <el-button type="primary" @click="router.push('/auth')">登录/注册</el-button>
          </template>
        </div>
      </el-header>

      <el-container>
        <el-aside width="200px" class="main-aside">
          <el-menu :default-active="activeMenu" class="main-menu" router>
            <el-menu-item index="/">
              <el-icon><data-analysis /></el-icon>
              <span>数据大屏</span>
            </el-menu-item>
            <el-menu-item index="/novels">
              <el-icon><reading /></el-icon>
              <span>小说列表</span>
            </el-menu-item>
            <el-menu-item index="/search">
              <el-icon><search /></el-icon>
              <span>搜索小说</span>
            </el-menu-item>
            <el-menu-item index="/upload">
              <el-icon><upload /></el-icon>
              <span>添加小说</span>
            </el-menu-item>
            <el-menu-item index="/tasks">
              <el-icon><document /></el-icon>
              <span>任务管理</span>
            </el-menu-item>
          </el-menu>
        </el-aside>

        <el-main class="main-content">
          <router-view />
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script setup lang="ts">
import { computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, DataAnalysis, Reading, Search, Upload, Document } from '@element-plus/icons-vue'
import { useAuthStore, useTaskStore } from '../store'
import api from '../api'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const taskStore = useTaskStore()

const activeMenu = computed(() => route.path)

// 处理用户下拉菜单命令
const handleCommand = (command: string) => {
  if (command === 'logout') {
    ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
      .then(() => {
        authStore.logout()

        // 断开WebSocket连接
        if (api.WebSocketAPI.isConnected()) {
          api.WebSocketAPI.disconnect()
        }

        ElMessage.success('已退出登录')
        router.push('/auth')
      })
      .catch(() => {})
  }
}

// 设置 WebSocket 连接
const setupWebSocketConnection = () => {
  const token = localStorage.getItem('accessToken')
  if (!token) return

  // 设置 WebSocket 连接以获取任务更新
  taskStore.setupWebSocketListener()

  console.log('WebSocket connection established in main app')
}

onMounted(() => {
  // 检查是否已登录
  authStore.checkAuth()

  // 如果已登录，设置WebSocket连接
  if (authStore.isAuthenticated) {
    setupWebSocketConnection()
  }
})

// 监听认证状态变化
watch(
  () => authStore.isAuthenticated,
  (isAuthenticated) => {
    if (isAuthenticated) {
      setupWebSocketConnection()
    } else {
      // 如果用户登出，断开WebSocket连接
      if (api.WebSocketAPI.isConnected()) {
        api.WebSocketAPI.disconnect()
      }
    }
  },
)
</script>

<style scoped>
.home-container {
  height: 100vh;
  overflow: hidden;
}

.main-header {
  background-color: #409eff;
  color: white;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  height: 60px;
}

.main-aside {
  background-color: #f5f7fa;
  height: calc(100vh - 60px);
  border-right: 1px solid #e6e6e6;
}

.main-menu {
  height: 100%;
  border-right: none;
}

.main-content {
  padding: 20px;
  height: calc(100vh - 60px);
  overflow-y: auto;
}

.user-dropdown {
  cursor: pointer;
  display: flex;
  align-items: center;
  color: white;
  font-size: 14px;
}

.user-dropdown .el-icon {
  margin-left: 5px;
}
</style>
